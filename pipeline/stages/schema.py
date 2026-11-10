"""Stage 3 - Schema design: LLM agent + deterministic lint + automated review gate.

  3a conceptual design   (LLM, model A)  relationships -> embed/reference decisions, collections
  3b logical design      (LLM, model A)  conceptual -> schema.json in the DSL (pipeline/schema_dsl.py)
  3c lint                (code)          R2 unbounded arrays, R4 self-reference, R7 16 MB, R8 recoverability, R10 indexes
     -> on errors: repair  (LLM, model A, max 3 rounds) with the exact lint messages
  3d critic              (LLM, model B - a DIFFERENT model, so it does not grade its own work)
     -> on blocking issues: revise (model A) and re-lint
  3e gate                schema must pass lint; a written self-review (design/self_review.md) must exist
                         before Stage 4 is allowed to start.
Outputs: design/schema.json, design/schema_naive.json, design/schema.md, design/conceptual.json
"""
import json

from pipeline import schema_dsl as dsl
from pipeline.config import settings
from pipeline.llm import LLM, extract_json

D = settings.root / "design"
SYSTEM = ("You are a senior MongoDB data modeler migrating a relational app. Follow the guidebook rules exactly. "
          "Answer with ONE JSON object and nothing else.")


def _load():
    inv = json.loads((settings.root / "analysis/inventory.json").read_text())
    pats = json.loads((settings.root / "analysis/access_patterns.json").read_text())
    return inv, pats


def conceptual(llm, guide, compact):
    prompt = f"""GUIDEBOOK:
{guide}

APPLICATION (Conduit, a Medium-like blog; Express + Sequelize + Postgres). Inventory produced by a static analyzer:
{compact}

TASK: CONCEPTUAL schema design. For EVERY relationship/junction table decide embed vs reference, citing rule ids.
Return JSON:
{{"relationships":[{{"relationship":"...","cardinality":"...","decision":"embed array|extended reference|junction collection|reference|counter",
  "rules":["R1",...],"reason":"one sentence tied to an access pattern"}}],
 "collections":[{{"name":"...","source":"<table>","contains":"what is embedded/denormalized"}}],
 "counters":[{{"collection":"...","field":"...","counts":"<table>","maintainedBy":"which writes update it"}}],
 "risks":["one line each"]}}"""
    return extract_json(llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                 purpose="schema.conceptual", max_tokens=3000, reasoning="medium", json_mode=True))


def logical(llm, concept, compact_tables, extra=""):
    prompt = f"""Translate this CONCEPTUAL design into the LOGICAL schema DSL below.
CONCEPTUAL: {json.dumps(concept, separators=(',', ':'))}

SOURCE TABLES:
{compact_tables}

DSL (schema.json):{dsl.DSL_SPEC}
Rules: "id" must be exactly the table's PK columns. Map EVERY source column of a collection's source table
(PK columns go in "id", FK columns either in "fields" or as an extendedRef localKey). A junction table that is not
an embedArray must be its own collection. Add every index the query predicates need (equality fields, then sort field
with -1). Field types: int, long, string, date, bool.{extra}
Return JSON: {{"collections":[...]}}"""
    return extract_json(llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                 purpose="schema.logical", max_tokens=3800, reasoning="low", json_mode=True))


def repair(llm, schema, errors, compact_tables):
    prompt = f"""This MongoDB schema (DSL) FAILED a deterministic lint. Fix ALL errors; change nothing else.
LINT ERRORS:
- """ + "\n- ".join(errors) + f"""

SOURCE TABLES:
{compact_tables}

DSL:{dsl.DSL_SPEC}
SCHEMA: {json.dumps(_strip(schema), separators=(',', ':'))}
Return the full corrected JSON {{"collections":[...]}}."""
    return extract_json(llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                 purpose="schema.repair", max_tokens=3000, reasoning="low", json_mode=True))


def critic(llm, schema, compact):
    prompt = f"""You are REVIEWING (not designing) a MongoDB schema proposed by another model for this app.
{compact}

PROPOSED SCHEMA: {json.dumps(_strip(schema, reasons=False), separators=(',', ':'))}

Check it against each access pattern: hot reads served without per-row lookups? unbounded arrays? self-reference?
counters kept consistent? missing/redundant indexes? API fields still producible (author profile, following,
favorited, favoritesCount, followersCount, tagList, comment ids)?
Return JSON {{"issues":[{{"severity":"blocking|minor","issue":"...","fix":"..."}}],"verdict":"approve|revise"}}.
Only mark "blocking" what would break correctness or the guidebook rules. In this DSL a multi-column "id" IS a
compound _id object (that is valid). At most 6 issues, each issue and fix under 40 words."""
    return extract_json(llm.chat([{"role": "system", "content": "You are a strict MongoDB schema reviewer. Answer with ONE JSON object."},
                                  {"role": "user", "content": prompt}],
                                 purpose="schema.critic", model=settings.llm_review_model, max_tokens=3000))


def _strip(schema, reasons=True):
    s = json.loads(json.dumps(schema))
    for c in s.get("collections", []):
        c.pop("_estimatedMaxDocBytes", None)
        if not reasons:
            for k in ("embedArrays", "extendedRefs", "counters"):
                for x in c.get(k, []):
                    x.pop("reason", None)
    return s


def run(state):
    inv, pats = _load()
    llm = LLM(state)
    guide = (D / "guidebook.md").read_text()
    compact = dsl.compact_inventory(inv, pats)
    compact_tables = compact.split("ASSOCIATIONS:")[0]
    profile = dsl.data_profile()
    log = {"lint_rounds": [], "critic": None}

    concept = conceptual(llm, guide, compact)
    (D / "conceptual.json").write_text(json.dumps(concept, indent=2))
    print(f"  conceptual: {len(concept.get('relationships', []))} relationship decisions, "
          f"{len(concept.get('collections', []))} collections")
    schema = logical(llm, concept, compact_tables)

    def lint_loop(schema, label):
        for rnd in range(4):
            E, W = dsl.lint(schema, inv, profile)
            log["lint_rounds"].append({"stage": label, "round": rnd, "errors": E, "warnings": W})
            print(f"  lint[{label} #{rnd}]: {len(E)} errors, {len(W)} warnings")
            if not E:
                return schema
            if rnd == 3:
                raise RuntimeError("schema still fails lint after 3 repairs:\n  " + "\n  ".join(E))
            schema = repair(llm, schema, E, compact_tables)
        return schema

    schema = lint_loop(schema, "initial")
    rev = critic(llm, schema, compact)
    log["critic"] = rev
    blocking = [i for i in rev.get("issues", []) if i.get("severity") == "blocking"]
    print(f"  critic ({settings.llm_review_model}): verdict={rev.get('verdict')} blocking={len(blocking)} "
          f"minor={len(rev.get('issues', [])) - len(blocking)}")
    if blocking:
        schema = repair(llm, schema, [f"[reviewer, apply only if valid under the guidebook] {i.get('issue')} => {i.get('fix')}"
                                      for i in blocking], compact_tables)
        schema = lint_loop(schema, "after-critic")

    # Self-review gate: findings from design/self_review.md ("## Required changes" bullets) are applied by the
    # designer model, then re-linted. The reviewer (Claude, a third model) never edits the JSON by hand.
    sr = D / "self_review.md"
    if sr.exists() and "## Required changes" in sr.read_text():
        req = [l[2:].strip() for l in sr.read_text().split("## Required changes", 1)[1].split("\n## ", 1)[0].splitlines()
               if l.startswith("- ")]
        if req:
            schema = repair(llm, schema, [f"[self-review] {r}" for r in req], compact_tables)
            schema = lint_loop(schema, "after-self-review")
            log["self_review_changes"] = req
    E, W = dsl.lint(schema, inv, profile)
    assert not E
    out = {"generatedBy": f"{settings.llm_model} (design) / {settings.llm_review_model} (critic), linted by pipeline/schema_dsl.py",
           **_strip(schema)}
    for c, orig in zip(out["collections"], schema["collections"]):
        c["estimatedMaxDocBytes"] = orig.get("_estimatedMaxDocBytes")
    (D / "schema.json").write_text(json.dumps(out, indent=2))
    naive = dsl.naive_schema(inv)
    (D / "schema_naive.json").write_text(json.dumps(naive, indent=2))
    nE, _ = dsl.lint(naive, inv, profile)
    log["naive_lint_errors"] = nE
    log["profile"] = profile
    (D / "schema_review_log.json").write_text(json.dumps(log, indent=2))
    write_markdown(concept, out, log, W)
    gate = (D / "self_review.md").exists()
    state.set_stage("schema", "done" if gate else "awaiting_self_review",
                    lint_rounds=len(log["lint_rounds"]), critic_blocking=len(blocking))
    if not gate:
        print("  GATE: schema passes lint; design/self_review.md (the automated-gate self-review) is required "
              "before Stage 4. Re-run `python -m pipeline schema` after writing it.")


def write_markdown(concept, schema, log, warnings):
    L = ["# MongoDB schema design", "",
         "Produced by Stage 3 (`python -m pipeline schema`). This file is GENERATED; the self-review is in "
         "[self_review.md](self_review.md).", "",
         f"Designed by `{settings.llm_model}`, criticized by `{settings.llm_review_model}`, and linted by "
         "deterministic code (`pipeline/schema_dsl.py`).", "",
         "## 1. Conceptual schema", "", "| Relationship | Cardinality | Decision | Rules | Why |", "|---|---|---|---|---|"]
    for r in concept.get("relationships", []):
        L.append(f"| {r.get('relationship')} | {r.get('cardinality')} | **{r.get('decision')}** | "
                 f"{', '.join(r.get('rules', []))} | {r.get('reason')} |")
    L += ["", "Collections:", ""] + [(f"- **{c.get('name')}** (from `{c.get('source')}`): {c.get('contains')}"
                                      if isinstance(c, dict) else f"- {c}") for c in concept.get("collections", [])]
    if concept.get("counters"):
        L += ["", "Denormalized counters:", ""] + [(f"- `{c.get('collection')}.{c.get('field')}` counts `{c.get('counts')}`; "
                                                     f"maintained by {c.get('maintainedBy')}") if isinstance(c, dict) else f"- {c}"
                                                    for c in concept["counters"]]
    if concept.get("risks"):
        L += ["", "Risks named by the designer:", ""] + [f"- {r}" for r in concept["risks"]]
    L += ["", "## 2. Logical schema (field mapping)", ""]
    for c in schema["collections"]:
        L += [f"### `{c['name']}`  (source table `{c['source']}`, `_id` from {c['id']}, est. max doc "
              f"{c.get('estimatedMaxDocBytes')} B)", "", "| Field | From | Type |", "|---|---|---|"]
        L += [f"| `{f['name']}` | `{c['source']}.{f['from']}` | {f.get('type')} |" for f in c.get("fields", [])]
        L += [f"| `{a['name']}[]` | `{a['fromTable']}.{a['valueColumn']}` (embedded, maxItems {a.get('maxItems')}) | array | "
              for a in c.get("embedArrays", [])]
        L += [f"| `{r['name']}` | `{r['fromTable']}` via `{r['localKey']}` copies {r.get('copy')} | extended ref |"
              for r in c.get("extendedRefs", [])]
        L += [f"| `{k['name']}` | count of `{k['fromTable']}` by `{k['parentKey']}` | int (computed) |"
              for k in c.get("counters", [])]
        L += ["", "Indexes: " + ", ".join(f"`{json.dumps(i['keys'])}`{' unique' if i.get('unique') else ''}"
                                          for i in c.get("indexes", [])), ""]
        for k in ("embedArrays", "extendedRefs", "counters"):
            for x in c.get(k, []):
                if x.get("reason"):
                    L.append(f"- *{x['name']}*: {x['reason']}")
        L.append("")
    L += ["## 3. Deterministic lint", ""]
    for r in log["lint_rounds"]:
        L.append(f"- {r['stage']} round {r['round']}: {len(r['errors'])} errors" +
                 ("".join(f"\n  - {e}" for e in r["errors"]) if r["errors"] else " - PASS"))
    if warnings:
        L += ["", "Warnings on the final schema:", ""] + [f"- {w}" for w in warnings]
    L += ["", f"For comparison, the naive one-to-one schema (`schema_naive.json`) fails {len(log['naive_lint_errors'])} "
          "index-coverage checks (R10). It is kept unindexed on purpose, as the paper's one-to-one baseline.", "",
          "## 4. Automated review gate (critic model)", ""]
    crit = log["critic"] or {}
    L.append(f"Verdict: **{crit.get('verdict')}**")
    for i in crit.get("issues", []):
        L.append(f"- [{i.get('severity')}] {i.get('issue')} → *{i.get('fix')}*")
    L += ["", "## 5. Data profile used by the lint (validation dataset)", "", "```json",
          json.dumps(log["profile"], indent=1), "```"]
    (D / "schema.md").write_text("\n".join(L) + "\n")
