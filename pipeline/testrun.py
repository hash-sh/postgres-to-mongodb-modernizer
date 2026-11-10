"""Test harness for the MongoDB backend (used by Stage 4 and Stage 5). Deterministic.

Before EVERY run: the frozen test suite hash and the API contract (route set) are verified.
A slice "passes" when:
  (a) every characterization step of scenarios with slice <= k equals the golden master, and
  (b) the official Hurl suite gives the SAME per-request result as on the original app, for every entry
      of the official files that belong to slices <= k (behavioural equivalence, not "more passing").
"""
import json
import re

from pymongo import MongoClient

from pipeline import apps, guard, httpcheck, official
from pipeline.config import settings
from tests.characterization.scenarios import SCENARIOS

TEST_DB = "conduit_ci"
GOLDEN = settings.root / "tests/characterization/golden_original.json"


def reset_test_db():
    db = MongoClient(settings.mongo_uri)[TEST_DB]
    for name in db.list_collection_names():
        db[name].delete_many({})  # keep indexes, drop data (counters restart at 1, like RESTART IDENTITY)


def run_slice(state, k, include_official=True):
    guard.verify_tests(state.stage("baseline")["manifest_hash"])
    add_hash = state.stage("baseline").get("addendum_manifest_hash")
    if add_hash:
        guard.verify_tests(add_hash, guard.ADDENDUM_DIRS, guard.ADDENDUM_MANIFEST)
    guard.verify_api_contract()
    golden = json.loads(GOLDEN.read_text())
    scen = [s for s in SCENARIOS if s["slice"] <= k]
    be = apps.mongo(db=TEST_DB)
    res = {"slice": k}
    try:
        reset_test_db()
        be.start()
        results = httpcheck.run_all(be.url, scen, reset=reset_test_db)
        exp = {s["name"]: golden[s["name"]] for s in scen}
        if add_hash:  # frozen addendum scenarios, graded by their own golden master recorded from the original
            from tests.characterization_addendum.scenarios import SCENARIOS as ADD
            add = [s for s in ADD if s["slice"] <= k]
            results.update(httpcheck.run_all(be.url, add, reset=reset_test_db))
            exp.update(json.loads((settings.root / "tests/characterization_addendum/golden_original.json").read_text()))
            exp = {n: exp[n] for n in results}
        p, t, fails = httpcheck.compare(exp, results)
        res.update(char_passed=p, char_total=t, char_failures=fails)
        if include_official:
            reset_test_db()
            off = official.run(be.url, max_slice=k)
            base = json.loads((settings.root / "reports/raw/official_original.json").read_text())["entries"]
            mism = sorted(e for e, ok in off["entries"].items() if base.get(e) != ok)
            res.update(official_passed=off["passed"], official_total=off["total"], official_mismatch=mism,
                       official_baseline_passed=sum(1 for e in off["entries"] if base.get(e)))
        res["server_errors"] = server_errors(be.tail_log(400))
    finally:
        be.stop()
    # Accepted deviations are an explicit, logged decision (state["accepted_deviations"]), never silent: they are
    # still reported in every result and in reports/results.md. Frozen tests are never modified or skipped.
    accepted = set(state.data.get("accepted_deviations", {}))
    res["official_mismatch_accepted"] = sorted(set(res.get("official_mismatch", [])) & accepted)
    res["ok"] = res["char_passed"] == res["char_total"] and not (set(res.get("official_mismatch", [])) - accepted)
    return res


APP_FLOW_ERRORS = ("NotFoundError", "UnauthorizedError", "ForbiddenError", "ValidationError", "FieldRequiredError",
                   "AlreadyTakenError")  # the app's own control-flow errors (become 401/403/404/422): not crashes


def server_errors(log):
    """Unique stack-trace heads of real crashes from the app log (our files only) - fed back to the repair agent."""
    out = []
    lines = log.splitlines()
    for i, l in enumerate(lines):
        if re.match(r"^\w*Error\b|^\s*(TypeError|ReferenceError|MongoServerError|CastError|ValidationError)", l) or "Error:" in l[:60]:
            frames = [x.strip() for x in lines[i + 1:i + 8] if "mongo-backend/" in x and "node_modules" not in x][:3]
            if l.strip().split(":")[0] in APP_FLOW_ERRORS:
                continue
            item = l.strip()[:200] + ("\n    " + "\n    ".join(f.split("mongo-backend/")[-1] for f in frames) if frames else "")
            if item not in out:
                out.append(item)
    return out[-8:]


def response_shapes(paths_prefixes=None):
    """Compact response shapes per endpoint from the frozen golden master (read-only spec for the agent)."""
    golden = json.loads(GOLDEN.read_text())
    inv = json.loads((settings.root / "analysis/inventory.json").read_text())
    routes = [(r["method"], r["path"], re.compile("^" + re.sub(r":\w+", r"[^/]+", r["path"]) + "$"))
              for r in inv["routes"] if r["path"] not in ("/", "/*any")]

    def shape(v):
        if isinstance(v, dict):
            return "{" + ",".join(f"{k}:{shape(v[k])}" for k in sorted(v)) + "}"
        if isinstance(v, list):
            return "[" + (shape(v[0]) if v else "") + "]"
        return {str: "str", int: "int", bool: "bool", type(None): "str|null", float: "num"}.get(type(v), "?")

    out = {}
    for steps in golden.values():
        for s in steps:
            if s["status"] >= 400:
                continue
            m, p = s["request"].split(" ", 1)
            p = p.split("?")[0]
            tpl = next((rp for rm, rp, rx in routes if rm == m and rx.match(p) and not (rp == "/api/articles/:slug" and p.endswith("/feed"))), p)
            key = f"{m} {tpl} -> {s['status']}"
            if paths_prefixes and not any(tpl.startswith(x) for x in paths_prefixes):
                continue
            sh = shape(s["body"])
            if len(sh) > len(out.get(key, "")):
                out[key] = sh
    return out
