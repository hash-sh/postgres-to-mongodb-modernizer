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
from pipeline.llm import LLM, RequestTooLarge

APP = settings.mongo_backend
LOGDIR = settings.logs / "transform"
RULES = (settings.root / "pipeline/prompts/porting_rules.md").read_text()
FILE_ORDER = ["index.js", "middleware/authentication.js", "helper/helpers.js"]
AGENT_ALLOWLIST = ["index.js", "controllers/*.js", "helper/helpers.js", "middleware/authentication.js", "routes/tags.js"]
# Function-level notes written during diagnosis (each one is recorded in state chunks[].diagnoses).
FUNC_NOTES = {
    ("controllers/articles.js", "articlesFeed"):
        "articlesCount uses the SAME Sequelize findAndCountAll semantics as allArticles: LEFT JOIN on tagList, so it is "
        "the SUM over the followed authors' articles of max(1, tagList.length) - compute it with the same $group/$max/"
        "$size aggregation allArticles uses, NOT countDocuments.",
}
EXTRA_NOTES = {
    "controllers/articles.js": "Sequelize `include` with `where` (author/tag filter) is an INNER JOIN: if ?author= names no "
                               "user or ?tag= matches nothing, the result is EMPTY (articles: [], count 0). Default order: "
                               "createdAt DESC. createArticle answers with tagList = the request's tagList as sent.",
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


# Sequelize-only APIs. If any survives in converted code it is a guaranteed runtime TypeError - caught statically.
SEQUELIZE_RESIDUE = re.compile(r"\.(getFollowing|getFollowers|getFavorites|countFavorites|getTagList|addTagList|hasUser|countUsers|"
                               r"addUser|removeUser|getAuthor|setAuthor|hasFollower|countFollowers|addFollower|removeFollower|"
                               r"getComments|findAndCountAll|findByPk|findAll)\(|\.dataValues\b|\bsequelize\b")
TRANSLATION = {}  # method -> one-line MongoDB equivalent, parsed from porting rule 10
for _l in RULES.split("10.", 1)[1].splitlines()[1:]:
    _m = re.match(r"\s+([\w.]+)\(.*?\)\s+->\s+(.*)", _l)
    if _m:
        TRANSLATION[_m.group(1).split(".")[-1]] = _l.strip()


class Transformer:
    def __init__(self, state):
        self.state = state
        self.llm = LLM(state)
        self.writer = guard.SafeWriter(APP, AGENT_ALLOWLIST)
        self.attempts = {}
        self.inv = json.loads((settings.root / "analysis/inventory.json").read_text())
        self.models_api = (settings.root / "design/models_api.txt").read_text()
        LOGDIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- context builders
    def notes_for(self, rel, funcs=None):
        notes = list(dict.fromkeys(n["text"] for n in self.inv["semanticNotes"]
                                   if n["file"] == rel and (funcs is None or n["function"] in funcs)))
        if rel in EXTRA_NOTES:
            notes.append(EXTRA_NOTES[rel])
        for (f, fn), text in FUNC_NOTES.items():
            if f == rel and (funcs is None or fn in funcs):
                notes.append(text)
        return notes

    def shapes_for(self, rel, funcs=None):
        eps = [r for r in self.inv["routes"] if (r["handler"]["file"] == rel or r["routeFile"] == rel)
               and (funcs is None or r["handler"]["function"] in funcs)]
        if rel in ("helper/helpers.js", "middleware/authentication.js"):
            return {}
        prefixes = sorted({r["path"].split(":")[0].rstrip("/") or r["path"] for r in eps})
        shapes = testrun.response_shapes(prefixes)
        mine = {(r["method"], r["path"]) for r in eps}
        return {k: v for k, v in shapes.items() if tuple(k.split(" -> ")[0].split(" ", 1)) in mine}

    def converted_context(self, rel):
        """Already-converted shared modules the file depends on (keeps conventions consistent across slices)."""
        out = []
        if rel != "middleware/authentication.js" and (APP / "middleware/authentication.js").exists():
            out.append("req.loggedUser (set by the converted auth middleware) is a lean User document WITHOUT email, "
                       "with `_id`, username, bio, image, password, followersCount and `token`; it is undefined when no "
                       "Authorization header was sent.")
        for dep in ("helper/helpers.js",):
            p = APP / dep
            if dep != rel and p.exists() and "require(\"../models\")" in p.read_text() and "Sequelize" not in p.read_text():
                src = (settings.original_backend / rel).read_text() if (settings.original_backend / rel).exists() else ""
                if dep == "helper/helpers.js" and "helpers" not in src and rel != "routes/tags.js":
                    continue
                out.append(f"--- already converted {dep} (use its exports as-is) ---\n{p.read_text()}")
        return "\n".join(out)

    def base_prompt(self, rel, funcs=None):
        shapes = self.shapes_for(rel, funcs)
        parts = [RULES, "MODELS (generated from the reviewed schema; you cannot change them):", self.models_api]
        if shapes:
            parts += ["RESPONSE SHAPES the endpoints of this file must produce (from the original app):"] + \
                     [f"- {k}: {v}" for k, v in shapes.items()]
        notes = self.notes_for(rel, funcs)
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

    # ---------------------------------------------------------------- function mode (large files)
    # A file above FUNC_MODE_CHARS does not fit one free-tier request (prompt + answer <= 8k tokens), so it is
    # converted and repaired one handler function at a time. Splitting/assembly is deterministic (Babel AST).
    FUNC_MODE_CHARS = 4000

    @staticmethod
    def segments(code):
        tmp = LOGDIR / "_split_tmp.js"
        tmp.write_text(code)
        r = subprocess.run(["node", str(settings.root / "analyzer/split.js"), str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError("cannot parse JS: " + r.stderr[-400:])
        return json.loads(r.stdout)

    def hints_for(self, rel, fname):
        """Analyzer-guided hints: the exact translation for every Sequelize call site in THIS original function."""
        out = []
        for cs in self.inv["callSites"]:
            if cs["file"] == rel and cs["function"] == fname:
                t = TRANSLATION.get(cs["method"]) or ("Model.findAndCountAll -> find + count (see ORM notes)" if cs["method"] == "findAndCountAll" else None)
                if t and t not in out:
                    out.append(f"line {cs['line']} `{cs['receiver']}.{cs['method']}(...)`: {t}")
        return ("REQUIRED TRANSLATIONS in this function (from the static analyzer):\n- " + "\n- ".join(out) + "\n") if out else ""

    def residue(self, rel):
        """Functions of the converted file that still call Sequelize-only APIs."""
        code = (APP / rel).read_text()
        if not self.big(rel):
            return ["<file>"] if SEQUELIZE_RESIDUE.search(code) else []
        return [x["name"] for x in self.segments(code) if x["kind"] == "function" and SEQUELIZE_RESIDUE.search(x["text"])]

    def big(self, rel):
        return len((settings.original_backend / rel).read_text()) > self.FUNC_MODE_CHARS

    REQ = re.compile(r"^(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\((['\"])([^'\"]+)\2\);?\s*$", re.S)

    def _merge_header(self, header, extra_segments):
        """Add new top-level statements to the header; destructured requires of an already-required module are
        MERGED into the existing statement (appending would redeclare identifiers)."""
        for seg in extra_segments:
            t = seg["text"].strip()
            if seg["kind"] != "other" or not t or t in header:
                continue
            m = self.REQ.match(t)
            if m:
                header = self._merge_require(header, m.group(3), [x.strip() for x in m.group(1).split(",") if x.strip()])
            else:
                header = header.rstrip() + "\n" + t + "\n"
        return header

    def _merge_require(self, header, module, names):
        segs = self.segments(header)
        for x in segs:
            m = self.REQ.match(x["text"].strip())
            if m and m.group(3) == module:
                have = [y.strip() for y in m.group(1).split(",") if y.strip()]
                allv = have + [n for n in names if n not in have]
                stmt = "const {\n" + "".join(f"  {n},\n" for n in allv) + f'}} = require("{module}");'
                return header.replace(x["text"], stmt)
        return header.rstrip() + "\n" + "const { " + ", ".join(names) + f' }} = require("{module}");\n'

    PROVIDED = {"../models": ["User", "Article", "Comment", "Tag", "Favorite", "Follower", "mongoose", "nextId"],
                "../helper/helpers": ["slugify", "appendTagList", "appendFavorites", "appendFollowers", "articleResponse"]}

    def fix_imports(self, rel):
        """Deterministic codemod (like an eslint no-undef autofix): a model / helper export that is USED but not
        imported is added to the existing destructured require of its module."""
        if not rel.startswith(("controllers/", "routes/")):
            return []
        code = (APP / rel).read_text()
        segs = self.segments(code)
        header = "\n".join(x["text"] for x in segs if x["kind"] == "other")
        body = "\n".join(x["text"] for x in segs if x["kind"] != "other")
        added = []
        for module, names in self.PROVIDED.items():
            if module == "../helper/helpers" and not (APP / "helper/helpers.js").read_text().count("articleResponse"):
                names = [n for n in names if n != "articleResponse"]
            missing = [n for n in names if re.search(rf"\b{n}\b", body) and not re.search(rf"\b{n}\b", header)]
            if missing:
                new_header = self._merge_require(header + "\n", module, missing)
                code = code.replace(header, new_header.rstrip(), 1)
                header = new_header
                added += missing
        if added:
            self.writer.write(rel, code)
            print(f"    import fixer: {rel} += {added}")
        return added

    def dedupe_requires(self, rel):
        """Deterministic repair of a header with the same module required twice (assembler bug fix)."""
        code = (APP / rel).read_text()
        first = code.find("const ")
        segs = self.segments(code)
        others = [x for x in segs if x["kind"] == "other"]
        header = ""
        for x in others:
            m = self.REQ.match(x["text"].strip())
            header = self._merge_require(header, m.group(3), [y.strip() for y in m.group(1).split(",") if y.strip()]) \
                if m and header else (header + x["text"].strip() + "\n")
        funcs = {x["name"]: x["text"] for x in segs if x["kind"] == "function"}
        exports = next(x["text"] for x in segs if x["kind"] == "exports")
        self.writer.write(rel, self._assemble(header, funcs, list(funcs), exports))

    def _assemble(self, header, funcs, order, exports):
        body = "\n\n".join(funcs[n] for n in order)
        return header.rstrip() + "\n\n" + body + "\n\n" + exports.strip() + "\n"

    def generate_funcs(self, chunk_id, rel):
        orig = (settings.original_backend / rel).read_text()
        segs = self.segments(orig)
        fsegs = [x for x in segs if x["kind"] == "function"]
        header_orig = "\n".join(x["text"] for x in segs if x["kind"] == "other")
        exports = next(x["text"] for x in segs if x["kind"] == "exports")
        groups, cur, size = [], [], 0
        for f in fsegs:
            if cur and size + len(f["text"]) > 2600:
                groups.append(cur)
                cur, size = [], 0
            cur.append(f)
            size += len(f["text"])
        groups.append(cur)
        header, funcs = None, {}
        for g in groups:
            names = [f["name"] for f in g]
            src = "\n\n".join(f["text"] for f in g)
            if header is None:
                task = (f"ORIGINAL MODULE HEADER of {rel}:\n```javascript\n{header_orig}\n```\nReturn the CONVERTED header "
                        f"(requires/shared consts) first, then the converted functions {names}.")
            else:
                task = f"The converted module header is:\n```javascript\n{header}```\nDo not repeat it. Return ONLY the converted functions {names}."
            prompt = (f"{self.base_prompt(rel, names)}\n\nYou convert PART of {rel}, function by function. {task}\n"
                      f"Keep each as `const NAME = async (req, res, next) => {{ ... }};`. Do NOT write module.exports.\n"
                      + "".join(self.hints_for(rel, n) for n in names) +
                      f"FUNCTIONS TO CONVERT:\n```javascript\n{src}\n```")
            reply = self.llm.chat([{"role": "user", "content": prompt}], purpose="transform.generate", chunk=chunk_id, max_tokens=3200)
            out = self.segments(extract_code(reply))
            if header is None:
                header = "\n".join(x["text"] for x in out if x["kind"] == "other") + "\n"
            else:
                header = self._merge_header(header, out)
            for x in out:
                if x["kind"] == "function":
                    funcs[x["name"]] = x["text"]
            missing = [n for n in names if n not in funcs]
            if missing:
                raise ValueError(f"LLM reply for {rel} is missing functions {missing}")
        self.writer.write(rel, self._assemble(header, funcs, [f["name"] for f in fsegs], exports))

    def repair_func(self, chunk_id, rel, fname, report, purpose="transform.repair", fresh=False):
        cur_segs = self.segments((APP / rel).read_text())
        orig_segs = self.segments((settings.original_backend / rel).read_text())
        header = "\n".join(x["text"] for x in cur_segs if x["kind"] == "other") + "\n"
        funcs = {x["name"]: x["text"] for x in cur_segs if x["kind"] == "function"}
        order = [x["name"] for x in cur_segs if x["kind"] == "function"]
        exports = next(x["text"] for x in cur_segs if x["kind"] == "exports")
        orig_f = next((x["text"] for x in orig_segs if x["name"] == fname), "")
        current = "" if fresh else f"CURRENT `{fname}`:\n```javascript\n{funcs.get(fname, '')}\n```\n"
        intro = (f"Convert function `{fname}` of {rel} AGAIN FROM SCRATCH (previous attempts kept failing; do not reuse "
                 f"them). These were their failures:" if fresh else
                 f"Function `{fname}` of {rel} FAILS frozen tests (expected = the original app's behaviour). Fix it.\nFAILURES:")
        prompt = (f"{self.base_prompt(rel, [fname])}\n\n{intro}\n{report[:2400]}\n\nMODULE HEADER (current, already "
                  f"converted):\n```javascript\n{header}```\n{self.hints_for(rel, fname)}ORIGINAL `{fname}`:\n```javascript\n{orig_f}\n```\n"
                  f"{current}Return ONLY the {'converted' if fresh else 'corrected'} function "
                  f"(prefix new require lines only if you need new imports).")
        try:
            reply = self.llm.chat([{"role": "user", "content": prompt}], purpose=purpose, chunk=chunk_id, max_tokens=2600)
        except RequestTooLarge:  # compact retry: shorter failure report, no analyzer notes
            prompt = prompt.replace(report[:2400], report[:900])
            reply = self.llm.chat([{"role": "user", "content": prompt}], purpose=purpose, chunk=chunk_id, max_tokens=2200)
        try:
            out = self.segments(extract_code(reply))
        except ValueError as e:  # invalid JS in the reply: one retry with the parser error, else keep the old function
            reply = self.llm.chat([{"role": "user", "content": prompt + f"\n\nYOUR PREVIOUS ANSWER WAS NOT VALID JAVASCRIPT "
                                    f"({str(e)[-200:]}). Return the complete, syntactically valid function."}],
                                  purpose=purpose, chunk=chunk_id, max_tokens=2600)
            try:
                out = self.segments(extract_code(reply))
            except ValueError:
                print(f"    [repair] {rel}#{fname}: reply still not valid JS, function left unchanged")
                return
        new = {x["name"]: x["text"] for x in out if x["kind"] == "function"}
        if fname not in new:
            print(f"    [repair] {rel}#{fname}: reply did not contain the function, left unchanged")
            return
        funcs[fname] = new[fname]
        header = self._merge_header(header, out)
        self.writer.write(rel, self._assemble(header, funcs, order, exports))

    def implicated_funcs(self, rel, res, k=3):
        """Top-k distinct failing handler functions (function mode repairs several per iteration)."""
        out = []
        for stale in range(6):
            f = self.implicated_func(rel, res, stale)
            if f not in out:
                out.append(f)
        return out[:k]

    def implicated_func(self, rel, res, stale=0):
        """Which handler function of `rel` to repair: first failing step per scenario weighs most."""
        score = {}
        cur = (APP / rel).read_text()
        segs = self.segments(cur)
        line_of = lambda pos: cur.count("\n", 0, pos) + 1
        ranges = [(x["name"], line_of(x["start"]), line_of(x["end"])) for x in segs if x["kind"] == "function"]
        routes = [(r["method"], re.compile("^" + re.sub(r":\w+", r"[^/]+", r["path"]) + "$"), r) for r in self.inv["routes"]]
        seen = set()
        for fl in res.get("char_failures", []):
            w = 5 if fl["scenario"] not in seen else 1
            seen.add(fl["scenario"])
            m, p = fl["request"].split(" ", 1)
            p = p.split("?")[0]
            for rm, rx, r in routes:
                if rm == m and rx.match(p):
                    if r["handler"]["file"] == rel:
                        score[r["handler"]["function"]] = score.get(r["handler"]["function"], 0) + w
                    break
        for e in res.get("official_mismatch", []) or []:
            m, p = self.official_request(e).split(" ", 1)
            p = p.split("?")[0]
            for rm, rx, r in routes:
                if rm == m and rx.match(p):
                    if r["handler"]["file"] == rel:
                        score[r["handler"]["function"]] = score.get(r["handler"]["function"], 0) + 5
                    break
        all_char_pass = res.get("char_passed") == res.get("char_total")
        for e in ([] if all_char_pass else res.get("server_errors", [])):  # once every step passes, remaining crashes
            for mt in re.finditer(re.escape(rel) + r":(\d+)", e):        # are the original's reproduced 500s
                ln = int(mt.group(1))
                for name, a, b in ranges:
                    if a <= ln <= b:
                        score[name] = score.get(name, 0) + 6
        ranked = sorted(score, key=lambda k: -score[k]) or [ranges[0][0]]
        return ranked[min(stale, len(ranked) - 1)]

    def add_serializer(self, cid):
        """Diagnosis (slice 2, round 7): each handler built article JSON by hand and each got it slightly wrong.
        Fix: ONE shared, precisely specified serializer, written by the agent into helper/helpers.js."""
        rel = "helper/helpers.js"
        cur = (APP / rel).read_text()
        prompt = (f"{RULES}\nMODELS:\n{self.models_api}\n\nAdd to {rel} an exported "
                  "`async function articleResponse(articleDoc, loggedUser)` (articleDoc = lean Article; loggedUser = lean "
                  "User or undefined) that returns EXACTLY {slug,title,description,body,createdAt,updatedAt,tagList,favorited,"
                  "favoritesCount,author:{username,bio,image,following,followersCount}}: author = User by articleDoc.authorId; "
                  "following = loggedUser follows the author (Follower _id {userId: author._id, followerId: loggedUser._id}), "
                  "false when no loggedUser; followersCount = author.followersCount; favorited = Favorite _id {articleId, "
                  "userId: loggedUser._id} exists (false without loggedUser); favoritesCount = articleDoc.favoritesCount; "
                  "tagList = articleDoc.tagList. Keep every existing export unchanged. Return the complete file.\n"
                  f"CURRENT {rel}:\n```javascript\n{cur}```")
        reply = self.llm.chat([{"role": "user", "content": prompt}], purpose="transform.serializer", chunk=cid, max_tokens=3000)
        code = extract_code(reply)
        if "articleResponse" not in code:
            raise ValueError("serializer reply lacks articleResponse")
        self.writer.write(rel, code)
        print("    added shared articleResponse() to helper/helpers.js")

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
    def official_request(entry):
        """'feed.hurl:110' -> 'GET /api/articles/feed?limit=1&offset=1' (the request line only, never the asserts)."""
        name, line = entry.split(":")
        txt = (settings.root / "tests/official/hurl" / name).read_text().splitlines()[int(line) - 1]
        return txt.replace("{{host}}", "").strip()

    @staticmethod
    def report(res, limit=10):
        L = [f"characterization: {res['char_passed']}/{res['char_total']} steps pass"]
        for f in res.get("char_failures", [])[:limit]:
            L.append(f"* [{f['scenario']}] {f['step']} ({f['request']}): " + "; ".join(f["diffs"][:4]))
        if res.get("official_mismatch"):
            base = json.loads((settings.root / "reports/raw/official_original.json").read_text())["entries"]
            for e in res["official_mismatch"][:6]:
                orig_ok = base.get(e)
                L.append(f"* official check `{Transformer.official_request(e)}` behaves differently: the ORIGINAL app "
                         f"{'PASSES' if orig_ok else 'FAILS'} it, the MongoDB app {'FAILS' if orig_ok else 'PASSES'} it "
                         f"(the MongoDB app must reproduce the original, including its quirks)")
        if res.get("server_errors"):
            L.append("server log errors:\n  " + "\n  ".join(res["server_errors"][:5]))
        return "\n".join(L)[:4000]

    # ---------------------------------------------------------------- chunk loop
    def run_chunk(self, chunk):
        cid, k = chunk["id"], chunk["slice"]
        files = [f for f in chunk_files(chunk) if f != "index.js" or k == 1]
        # Shared modules converted in an earlier slice stay repairable: later slices' behaviour depends on them,
        # and the earlier slices' tests are re-run on every later slice, so a regression is caught.
        for shared in ("helper/helpers.js", "middleware/authentication.js"):
            if shared not in files and (APP / shared).exists() and k > 1:
                files.append(shared)
        gen_files = [f for f in chunk_files(chunk) if f != "index.js" or k == 1]
        st = self.state.chunk(cid)
        if st["status"] == "done":
            print(f"  {cid}: already done")
            return True
        rnd = st.get("round", 0) + 1
        self.state.save_chunk(cid, status="running", round=rnd, files=files)
        if k == 2 and not st.get("serializer"):
            self.add_serializer(cid)
            for rel in gen_files:  # regenerate every article handler fresh against the shared serializer
                if self.big(rel):
                    self.dedupe_requires(rel)
                    for fn in [x["name"] for x in self.segments((APP / rel).read_text()) if x["kind"] == "function"]:
                        print(f"    regenerating {rel}#{fn} against articleResponse()")
                        self.repair_func(cid, rel, fn, "Rewrite using articleResponse() (rule 11).", fresh=True)
            self.state.save_chunk(cid, serializer=True)
        if not st.get("generated"):
            for rel in gen_files:
                print(f"  {cid}: generating {rel}" + (" (function by function)" if self.big(rel) else ""))
                (self.generate_funcs if self.big(rel) else self.generate)(cid, rel)
            self.state.save_chunk(cid, generated=True)
        last_pass, stale = -1, 0
        best = {"passed": -1, "files": {}}  # keep-best guard: never continue from a version that scores lower
        for it in range(settings.llm_max_repair_iters + 1):
            for f in files:
                if self.big(f):
                    self.dedupe_requires(f)  # deterministic, idempotent header normalization
                if self.fix_imports(f):
                    st.setdefault("import_fixes", []).append(f)
            for f in files:  # deterministic Sequelize-residue lint: regenerate offending functions fresh
                for fn in self.residue(f):
                    if fn == "<file>":
                        continue
                    print(f"    residue lint: {f}#{fn} still calls Sequelize APIs -> fresh regeneration")
                    found = sorted(set(m.group(0) for m in SEQUELIZE_RESIDUE.finditer(
                        next(x["text"] for x in self.segments((APP / f).read_text()) if x["name"] == fn))))
                    self.repair_func(cid, f, fn, "This function still calls Sequelize-only APIs that no longer exist: "
                                     + ", ".join(found), fresh=True)
                    st.setdefault("residue_regenerations", []).append(f"{f}#{fn}")
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
            score = (res.get("char_passed") or 0) - len(res.get("official_mismatch", []) or [])
            if score >= best["passed"]:
                best = {"passed": score, "files": {f: (APP / f).read_text() for f in files}}
            elif best["files"]:
                for f, txt in best["files"].items():
                    if (APP / f).read_text() != txt:
                        self.writer.write(f, txt)
                entry["reverted_to_best"] = best["passed"]
                print(f"    keep-best: score {score} < best {best['passed']}, reverted to the best version")
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
            if self.big(target) and not syn and "boot_error" not in res:
                fnames = self.implicated_funcs(target, res)
                for fname in fnames:
                    # Anti-anchoring: a function repaired twice without progress is regenerated from the ORIGINAL,
                    # without showing the model its own broken attempt again.
                    key = f"{target}#{fname}"
                    self.attempts[key] = self.attempts.get(key, 0) + 1 if stale else 1
                    fresh = self.attempts[key] >= 2
                    print(f"    repairing {key}" + (" (fresh regeneration)" if fresh else ""))
                    self.repair_func(cid, target, fname, rep, fresh=fresh)
                    if fresh:
                        self.attempts[key] = 0
                target = ",".join(f"{target}#{f}" for f in fnames)
            else:
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
        if not (st.get("status") == "running" and backup.exists()):  # resume keeps the TRUE pre-optimization copy
            if backup.exists():
                shutil.rmtree(backup)
            for f in targets:
                (backup / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(APP / f, backup / f)
        self.state.save_chunk("optimization_pass", status="running", files=targets)
        done = st.setdefault("optimized", [])
        task = ("OPTIMIZATION PASS (behaviour must stay identical): this file is a correct translation but keeps a "
                "RELATIONAL SHAPE. Rewrite its data access to use the document model: no per-row queries inside loops "
                "(N+1) - read the page of articles once, then batch-load what they need with ONE `$in` query per "
                "collection (authors from users, which already store followersCount; the viewer's follows and "
                "favorites for those ids), use the embedded `tagList` and the stored `favoritesCount`/`followersCount` "
                "counters instead of counting junction documents. Keep exports, response shapes, ordering, error order.")
        for f in targets:
            if self.big(f):
                for fn in [x["name"] for x in self.segments((APP / f).read_text()) if x["kind"] == "function"]:
                    if f"{f}#{fn}" in done:
                        continue
                    print(f"  optimization: rewriting {f}#{fn}")
                    self.repair_func("optimization_pass", f, fn, task, purpose="transform.optimize")
                    done.append(f"{f}#{fn}")
                    self.state.save()
            elif f not in done:
                print(f"  optimization: rewriting {f}")
                try:
                    self.repair("optimization_pass", f, task, purpose="transform.optimize")
                except ValueError as e:  # unusable reply (e.g. truncated): keep the file as it is
                    print(f"  optimization: {f} left unchanged ({e})")
                done.append(f)
                self.state.save()
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
            if self.big(tgt):
                self.repair_func("optimization_pass", tgt, self.implicated_func(tgt, res), self.report(res),
                                 purpose="transform.optimize_repair")
            else:
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
