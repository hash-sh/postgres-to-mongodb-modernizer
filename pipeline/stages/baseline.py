"""Stage 1 - Baseline (deterministic).

1. Run the ORIGINAL app on Postgres.
2. Run the official RealWorld Hurl suite twice (to detect flakiness) and record the pass rate.
3. Record characterization tests (golden master) against the original, twice, and require
   both recordings to be identical (proves the tests + normalization are deterministic).
4. Freeze: hash every test file into tests/MANIFEST.sha256 BEFORE any transformation.
"""
import json

from pipeline import apps, guard, httpcheck, official
from pipeline.config import settings
from tests.characterization.scenarios import SCENARIOS

GOLDEN = settings.root / "tests/characterization/golden_original.json"
OUT = settings.root / "reports/raw"


def run(state, refreeze=False):
    OUT.mkdir(parents=True, exist_ok=True)
    if guard.MANIFEST.exists() and state.stage("baseline").get("manifest_hash") and not refreeze:
        guard.verify_tests(state.stage("baseline")["manifest_hash"])
        print("  test suite already frozen and verified; re-measuring official baseline only")
    with apps.original() as be:
        runs = []
        for i in range(2):
            apps.reset_postgres()
            r = official.run(be.url)
            runs.append(r)
            print(f"  official suite run {i + 1}: {r['passed']}/{r['total']} entries, {r['files_passed']}/{r['files']} files")
        flaky = sorted(k for k in runs[0]["entries"] if runs[0]["entries"][k] != runs[1]["entries"].get(k))
        (OUT / "official_original.json").write_text(json.dumps({**runs[0], "flaky_entries": flaky}, indent=2))

        if not GOLDEN.exists() or refreeze:
            recs = [httpcheck.run_all(be.url, SCENARIOS, reset=apps.reset_postgres) for _ in range(2)]
            if recs[0] != recs[1]:
                bad = [(n, a["step"], httpcheck.diff(a, b)[:3]) for n in recs[0]
                       for a, b in zip(recs[0][n], recs[1][n]) if a != b]
                raise RuntimeError(f"characterization recording is not deterministic: {bad}")
            GOLDEN.write_text(json.dumps(recs[0], indent=1, ensure_ascii=False, sort_keys=True))
            print(f"  golden master recorded: {sum(len(v) for v in recs[0].values())} steps in {len(recs[0])} scenarios")

    if not state.stage("baseline").get("manifest_hash") or refreeze:
        h = guard.freeze()
        state.set_stage("baseline", "running", manifest_hash=h)
        print(f"  test suite FROZEN: tests/MANIFEST.sha256 sha256={h[:16]}...")
    state.set_stage("baseline", "done", official_passed=runs[0]["passed"], official_total=runs[0]["total"],
                    official_flaky=len(flaky), characterization_steps=sum(len(s["steps"]) for s in SCENARIOS))
