"""Stage 4 - Transformation: LLM agent with a repair loop, chunk by chunk (vertical slices).

For each chunk (from analysis/chunks.json):
  1. generate: one LLM call per file (free tier: one file fits in an 8k-token request)
  2. syntax check (node --check)
  3. test: frozen characterization tests + official suite for slices <= k (pipeline/testrun.py)
  4. repair: failures (diffs + server stack traces) are fed back for the most-implicated file;
     max LLM_MAX_REPAIR_ITERS iterations per round. Every iteration is logged.
  If the cap is hit the stage stops with status "needs_diagnosis" - the orchestrating engineer
  diagnoses the root cause (chunking / schema / prompt), fixes THAT, and re-runs (a new round).
Then an optimization pass rewrites relational-shaped data access (per-row queries) to use the
embedded structure; if it breaks behaviour and can't be repaired, it is reverted.

Anti-cheating: the agent only returns text. pipeline.guard.SafeWriter writes it, and only to the
data-layer allowlist. Tests are hash-verified before every run (inside testrun.run_slice).
"""
import json
import re
import shutil
import subprocess
import time

from pipeline import codegen, guard, testrun
from pipeline.config import settings
from pipeline.llm import LLM

APP = settings.mongo_backend
LOGDIR = settings.logs / "transform"
RULES = (settings.root / "pipeline/prompts/porting_rules.md").read_text()
FILE_ORDER = ["index.js", "middleware/authentication.js", "helper/helpers.js"]
AGENT_ALLOWLIST = ["index.js", "controllers/*.js", "helper/helpers.js", "middleware/authentication.js", "routes/tags.js"]
EXTRA_NOTES = {
    "index.js": "Replace the sequelize.sync() bootstrap with `await connect()` from require('./models'); keep every app.use/route/listen line identical.",
    "middleware/authentication.js": "This defines req.loggedUser for all controllers. Keep it a lean user object INCLUDING `_id` and `password` (controllers need them) plus `token`; email is NOT included (the original excluded it) - headers.email is set as before.",
}


def chunk_files(chunk):
    return sorted(chunk["files"], key=lambda f: (FILE_ORDER.index(f) if f in FILE_ORDER else 99, f))


def extract_code(text):
    m = re.findall(r"```(?:javascript|js)?\s*\n(.*?)```", text, re.S)
    if m:
        return max(m, key=len).strip() + "\n"
    if "require(" in text or "module.exports" in text:
        return text.strip() + "\n"
    raise ValueError("no code block in LLM reply")


def node_check(path):
    r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    return None if r.returncode == 0 else r.stderr.strip()[-800:]


class Transformer:
    def __init__(self, state):
        self.state = state
        self.llm = LLM(state)
        self.writer = guard.SafeWriter(APP, AGENT_ALLOWLIST)
        self.inv = json.loads((settings.root / "analysis/inventory.json").read_text())
        self.models_api = (settings.root / "design/models_api.txt").read_text()
        LOGDIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- context builders
    def notes_for(self, rel):
        notes = [n["text"] for n in self.inv["semanticNotes"] if n["file"] == rel]
        if rel in EXTRA_NOTES:
            notes.append(EXTRA_NOTES[rel])
        return notes

    def shapes_for(self, rel):
        eps = [r for r in self.inv["routes"] if r["handler"]["file"] == rel or r["routeFile"] == rel]
        if rel in ("helper/helpers.js", "middleware/authentication.js"):
            return {}
        prefixes = sorted({r["path"].split(":")[0].rstrip("/") or r["path"] for r in eps})
        shapes = testrun.response_shapes(prefixes)
        mine = {(r["method"], r["path"]) for r in eps}
        return {k: v for k, v in shapes.items() if tuple(k.split(" -> ")[0].split(" ", 1)) in mine}

    def converted_context(self, rel):
        """Already-converted shared modules the file depends on (keeps conventions consistent across slices)."""
        out = []
        for dep in ("middleware/authentication.js", "helper/helpers.js"):
            p = APP / dep
            if dep != rel and p.exists() and "require(\"../models\")" in p.read_text() and "Sequelize" not in p.read_text():
                src = (settings.original_backend / rel).read_text() if (settings.original_backend / rel).exists() else ""
                if dep == "helper/helpers.js" and "helpers" not in src and rel != "routes/tags.js":
                    continue
                out.append(f"--- already converted {dep} (use its exports as-is) ---\n{p.read_text()}")
        return "\n".join(out)

    def base_prompt(self, rel):
        shapes = self.shapes_for(rel)
        parts = [RULES, "MODELS (generated from the reviewed schema; you cannot change them):", self.models_api]
        if shapes:
            parts += ["RESPONSE SHAPES the endpoints of this file must produce (from the original app):"] + \
                     [f"- {k}: {v}" for k, v in shapes.items()]
        notes = self.notes_for(rel)
        if notes:
            parts += ["ORM BEHAVIOUR TO PRESERVE (found by the static analyzer):"] + [f"- {n}" for n in notes]
        ctx = self.converted_context(rel)
        if ctx:
            parts.append(ctx)
        return "\n".join(parts)

    # ---------------------------------------------------------------- LLM actions
    def generate(self, chunk_id, rel):
        orig = (settings.original_backend / rel).read_text()
        prompt = f"{self.base_prompt(rel)}\n\nFILE TO CONVERT: {rel}\n```javascript\n{orig}```"
        reply = self.llm.chat([{"role": "user", "content": prompt}], purpose="transform.generate", chunk=chunk_id,
                              max_tokens=4200 if len(orig) > 4000 else 3000)
        self.writer.write(rel, extract_code(reply))

    def repair(self, chunk_id, rel, report, purpose="transform.repair"):
        cur = (APP / rel).read_text()
        orig = (settings.original_backend / rel).read_text()
        prompt = (f"{self.base_prompt(rel)}\n\nThe converted file {rel} FAILS frozen tests (expected = the original app's "
                  f"behaviour). Fix it. Return the complete corrected file.\nFAILURES:\n{report}\n\n"
                  f"ORIGINAL (Sequelize) {rel}:\n```javascript\n{orig}```\nCURRENT (Mongoose) {rel}:\n```javascript\n{cur}```")
        if len(prompt) > 17500:  # keep the request inside the free-tier 8k tokens/min window
            prompt = (f"{self.base_prompt(rel)}\n\nThe converted file {rel} FAILS frozen tests. Fix it, return the "
                      f"complete file.\nFAILURES:\n{report[:2500]}\nCURRENT {rel}:\n```javascript\n{cur}```")
        reply = self.llm.chat([{"role": "user", "content": prompt}], purpose=purpose, chunk=chunk_id,
                              max_tokens=4200 if len(cur) > 4000 else 3000)
        self.writer.write(rel, extract_code(reply))

    # ---------------------------------------------------------------- failure analysis
    def implicated(self, chunk, res, files):
        score = {f: 0 for f in files}
        routes = [(r["method"], re.compile("^" + re.sub(r":\w+", r"[^/]+", r["path"]) + "$"), r) for r in self.inv["routes"]]
        seen_scen = set()
        for fl in res.get("char_failures", []):
            # The FIRST failing step of a scenario is the likely root cause; later steps usually cascade from it
            # (e.g. registration fails => every authenticated step fails).
            w = 5 if fl["scenario"] not in seen_scen else 1
            seen_scen.add(fl["scenario"])
            m, p = fl["request"].split(" ", 1)
            p = p.split("?")[0]
            for rm, rx, r in routes:
                if rm == m and rx.match(p) and r["path"] not in ("/*any",):
                    h = r["handler"]["file"]
                    if h in score:
                        score[h] += 3 * w
                    if r["routeFile"] in score:
                        score[r["routeFile"]] += w
                    for helper in ("helper/helpers.js", "middleware/authentication.js"):
                        if helper in score and any(x in " ".join(fl["diffs"]) for x in ("following", "followersCount", "favorited", "favoritesCount", "status: expected 401")):
                            score[helper] += 1
                    break
        app_errors = ("AlreadyTakenError", "NotFoundError", "ValidationError", "UnauthorizedError", "ForbiddenError",
                      "FieldRequiredError")
        for e in res.get("server_errors", []):
            if e.split(":")[0].strip() in app_errors:
                continue  # normal control flow of the app, not a crash
            first_frame = next((f for f in files if f in e.split("\n", 2)[1]), None) if "\n" in e else None
            for f in files:
                if f in e:
                    score[f] += 12 if f == first_frame else 4
        ranked = sorted(files, key=lambda f: -score[f])
        return [f for f in ranked if score[f] > 0] or ranked[:1]

    @staticmethod
    def report(res, limit=10):
        L = [f"characterization: {res['char_passed']}/{res['char_total']} steps pass"]
        for f in res.get("char_failures", [])[:limit]:
            L.append(f"* [{f['scenario']}] {f['step']} ({f['request']}): " + "; ".join(f["diffs"][:4]))
        if res.get("official_mismatch"):
            L.append(f"official suite entries behaving differently from the original: {res['official_mismatch'][:8]}")
        if res.get("server_errors"):
            L.append("server log errors:\n  " + "\n  ".join(res["server_errors"][:5]))
        return "\n".join(L)[:4000]

    # ---------------------------------------------------------------- chunk loop
    def run_chunk(self, chunk):
        cid, k = chunk["id"], chunk["slice"]
        files = [f for f in chunk_files(chunk) if f != "index.js" or k == 1]
        st = self.state.chunk(cid)
        if st["status"] == "done":
            print(f"  {cid}: already done")
            return True
        rnd = st.get("round", 0) + 1
        self.state.save_chunk(cid, status="running", round=rnd, files=files)
        if not st.get("generated"):
            for rel in files:
                print(f"  {cid}: generating {rel}")
                self.generate(cid, rel)
            self.state.save_chunk(cid, generated=True)
        last_pass, stale = -1, 0
        for it in range(settings.llm_max_repair_iters + 1):
            syn = [(f, e) for f in files if (e := node_check(APP / f))]
            if syn:
                res = {"char_passed": 0, "char_total": 0, "syntax_errors": syn}
                rep = "SYNTAX ERROR (node --check):\n" + syn[0][1]
                target = syn[0][0]
            else:
                try:
                    res = testrun.run_slice(self.state, k)
                except RuntimeError as e:  # backend failed to boot
                    res = {"char_passed": 0, "char_total": 0, "boot_error": str(e)}
                    log = (settings.logs / "mongo_backend.log").read_text(errors="replace").splitlines()[-25:]
                    rep = "THE SERVER CRASHED ON STARTUP:\n" + "\n".join(log)
                    target = next((f for f in files if any(f in l for l in log)), files[0])
                else:
                    rep = self.report(res)
                    target = None
            entry = {"round": rnd, "iteration": it, "time": time.strftime("%H:%M:%S"),
                     "char": f"{res.get('char_passed')}/{res.get('char_total')}",
                     "official_mismatch": len(res.get("official_mismatch", [])) if "official_mismatch" in res else None,
                     "llm_tokens_total": self.state.data["llm"]["tokens"]}
            (LOGDIR / f"{cid}_r{rnd}_i{it}.json").write_text(json.dumps({**entry, "result": res}, indent=1, default=str))
            print(f"  {cid} round {rnd} iter {it}: char {entry['char']} official-mismatch {entry['official_mismatch']}")
            if res.get("ok"):
                st["iterations"].append({**entry, "repaired": None})
                self.state.save_chunk(cid, status="done", result={k2: res[k2] for k2 in ("char_passed", "char_total", "official_passed", "official_total")})
                return True
            if it == settings.llm_max_repair_iters:
                st["iterations"].append({**entry, "repaired": None})
                break
            if target is None:
                ranked = self.implicated(chunk, res, files)
                p = res.get("char_passed", 0)
                stale = stale + 1 if p <= last_pass else 0
                last_pass = max(last_pass, p)
                target = ranked[min(stale, len(ranked) - 1)]
            print(f"    repairing {target}")
            self.repair(cid, target, rep)
            st["iterations"].append({**entry, "repaired": target})
            self.state.save()
        self.state.save_chunk(cid, status="needs_diagnosis")
        return False

    # ---------------------------------------------------------------- optimization pass
    def optimize(self, chunks):
        st = self.state.chunk("optimization_pass")
        if st["status"] == "done":
            return True
        targets = [f for f in ("controllers/articles.js", "helper/helpers.js", "controllers/comments.js",
                               "controllers/favorites.js") if (APP / f).exists()]
        backup = LOGDIR / "pre_optimization"
        if backup.exists():
            shutil.rmtree(backup)
        for f in targets:
            (backup / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(APP / f, backup / f)
        self.state.save_chunk("optimization_pass", status="running", files=targets)
        task = ("OPTIMIZATION PASS (behaviour must stay identical): this file is a correct translation but keeps a "
                "RELATIONAL SHAPE. Rewrite its data access to use the document model: no per-row queries inside loops "
                "(N+1) - read the page of articles once, then batch-load what they need with ONE `$in` query per "
                "collection (authors from users, which already store followersCount; the viewer's follows and "
                "favorites for those ids), use the embedded `tagList` and the stored `favoritesCount`/`followersCount` "
                "counters instead of counting junction documents. Keep exports, response shapes, ordering, error order.")
        for f in targets:
            print(f"  optimization: rewriting {f}")
            self.repair("optimization_pass", f, task, purpose="transform.optimize")
        res = None
        for it in range(3):
            syn = [(f, e) for f in targets if (e := node_check(APP / f))]
            if syn:
                self.repair("optimization_pass", syn[0][0], "SYNTAX ERROR (node --check):\n" + syn[0][1], purpose="transform.optimize_repair")
                continue
            res = testrun.run_slice(self.state, 3)
            print(f"  optimization iter {it}: char {res['char_passed']}/{res['char_total']} "
                  f"official-mismatch {len(res.get('official_mismatch', []))}")
            st["iterations"].append({"iteration": it, "char": f"{res['char_passed']}/{res['char_total']}",
                                     "official_mismatch": len(res.get("official_mismatch", []))})
            if res["ok"]:
                self.state.save_chunk("optimization_pass", status="done", reverted=False)
                return True
            tgt = self.implicated({"files": targets}, res, targets)[0]
            self.repair("optimization_pass", tgt, self.report(res), purpose="transform.optimize_repair")
        # could not keep behaviour identical: revert (correctness beats speed), and record it honestly
        for f in targets:
            shutil.copy2(backup / f, APP / f)
        self.state.save_chunk("optimization_pass", status="done", reverted=True,
                              last=res and f"{res['char_passed']}/{res['char_total']}")
        print("  optimization pass REVERTED (could not preserve behaviour)")
        return True


def run(state):
    schema = json.loads((settings.root / "design/schema.json").read_text())
    if codegen.scaffold(force=False):
        print("  scaffolded apps/mongo-backend")
    codegen.gen_models(schema)
    t = Transformer(state)
    chunks = json.loads((settings.root / "analysis/chunks.json").read_text())
    for c in chunks:
        if not t.run_chunk(c):
            state.set_stage("transform", "needs_diagnosis", error=f"chunk {c['id']} hit the repair cap")
            print(f"  STOP: {c['id']} hit the repair cap; diagnose the root cause, fix it, re-run `python -m pipeline transform`")
            return
    t.optimize(chunks)
    final = testrun.run_slice(state, 3)
    state.set_stage("transform", "done" if final["ok"] else "failed",
                    final_char=f"{final['char_passed']}/{final['char_total']}",
                    final_official_mismatch=len(final.get("official_mismatch", [])))
    (settings.logs / "transform/final.json").write_text(json.dumps(final, indent=1, default=str))
