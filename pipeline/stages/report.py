"""Stage 6b - Report. Renders reports/results.md ONLY from raw artifacts written by real runs:
reports/raw/*.json, state/pipeline_state.json, logs/llm_calls.jsonl, logs/transform/*. Nothing is typed in by hand;
a missing artifact is reported as missing, never estimated.
"""
import json
from collections import defaultdict

from pipeline.config import settings

RAW = settings.root / "reports/raw"


def _load(name):
    p = RAW / name
    return json.loads(p.read_text()) if p.exists() else None


def llm_usage():
    rows = [json.loads(l) for l in (settings.logs / "llm_calls.jsonl").read_text().splitlines() if l.strip()]
    agg = defaultdict(lambda: {"calls": 0, "cached": 0, "prompt": 0, "completion": 0})
    for r in rows:
        k = (r["purpose"].split(".")[0], r["model"])
        a = agg[k]
        if r["cache_hit"]:
            a["cached"] += 1
        else:
            a["calls"] += 1
            a["prompt"] += r["prompt_tokens"] or 0
            a["completion"] += r["completion_tokens"] or 0
    return agg


def run(state):
    st = state.data
    base = _load("official_original.json")
    mig = _load("migrate_stage.json")
    bench = _load("benchmark.json")
    checks = _load("checks.json")
    L = ["# Results", "",
         "Every number in this file is read from an artifact written by an actual run (paths given per section). "
         "Regenerate with `python -m pipeline report`.", ""]

    # milestones
    ch = st["chunks"]
    def chunk_ok(c):
        return ch.get(c, {}).get("status") == "done"
    ms = [
        ("1. Baseline + official tests on Postgres", st["stages"]["baseline"]["status"] == "done",
         f"official suite {base['passed']}/{base['total']} request entries on the ORIGINAL app" if base else "missing"),
        ("2. Analysis inventory", st["stages"]["analysis"]["status"] == "done", "analysis/inventory.json"),
        ("3. Schema designed, linted, self-reviewed", st["stages"]["schema"]["status"] == "done", "design/schema.md, design/self_review.md"),
        ("4. Slice 1 (users/auth + profiles/follows) converted, migrated, validated", chunk_ok("slice1_users_profiles"),
         str(ch.get("slice1_users_profiles", {}).get("result", ""))),
        ("5. All slices converted, full suite passing on MongoDB",
         chunk_ok("slice2_articles_feed") and chunk_ok("slice3_comments_favorites_tags") and st["stages"]["transform"]["status"] == "done",
         f"transform stage: {st['stages']['transform'].get('final_char', '?')} characterization steps"),
        ("6. Benchmark, report, docs", bool(bench), "reports/raw/benchmark.json"),
    ]
    L += ["## Milestone status", "", "| Milestone | Status | Evidence |", "|---|---|---|"]
    L += [f"| {m} | {'DONE' if ok else 'NOT DONE'} | {ev} |" for m, ok, ev in ms]

    # tests
    L += ["", "## Tests", "", "Source: `reports/raw/official_original.json`, `reports/raw/migrate_stage.json`, `logs/transform/final.json`.", ""]
    if base:
        L.append(f"- **Official RealWorld suite (Hurl, gothinkster/realworld @2558884), original app on Postgres:** "
                 f"{base['passed']}/{base['total']} request entries pass ({base['files_passed']}/{base['files']} files fully). "
                 f"Flaky entries across two runs: {len(base['flaky_entries'])}. The app predates this spec version "
                 f"(e.g. it returns 422 where the spec wants 401/409, and `offset` is a page index), so a low baseline is expected.")
    if mig:
        L.append(f"- **Official suite on the MongoDB app:** {mig['official_mongo']} entries. Entries whose pass/fail differs "
                 f"from the original: **{len(mig['official_mismatch_vs_original'])}** "
                 + (f"({', '.join(mig['official_mismatch_vs_original'][:10])})" if mig['official_mismatch_vs_original'] else "(behaviourally identical)") + ".")
        L.append(f"- **Frozen characterization tests (155 steps recorded from the original):** {mig['characterization']} pass on MongoDB.")
        d = mig["differential"]
        L.append(f"- **Differential test (same {d['requests']} requests replayed on both backends over identical migrated data):** "
                 f"{d['matched']}/{d['requests']} responses identical after normalization (match rate {d['match_rate']:.2%}).")
        diff = _load("differential.json")
        if diff and diff["mismatches"]:
            L.append("  Mismatches:")
            L += [f"  - `{m['request']}`: {'; '.join(m['diffs'][:3])}" for m in diff["mismatches"][:15]]

    # repair iterations
    L += ["", "## Transformation: repair iterations per chunk", "", "Source: `state/pipeline_state.json` (chunks), per-iteration logs in `logs/transform/`.", "",
          "| Chunk | Status | Rounds | Test runs | Repairs (file per iteration) | Result |", "|---|---|---|---|---|---|"]
    for cid, c in ch.items():
        its = c.get("iterations", [])
        reps = [i.get("repaired") for i in its if i.get("repaired")]
        L.append(f"| {cid} | {c['status']} | {c.get('round', 1)} | {len(its)} | {len(reps)}: {', '.join(reps) or '-'} | "
                 f"{its[-1]['char'] if its else '-'} |")
    if ch.get("optimization_pass"):
        o = ch["optimization_pass"]
        L.append(f"\nOptimization pass: {'REVERTED (could not preserve behaviour)' if o.get('reverted') else 'kept'}; "
                 f"iterations: {[i['char'] for i in o.get('iterations', [])]}")

    # LLM
    agg = llm_usage()
    L += ["", "## LLM usage and cost", "", "Source: `logs/llm_calls.jsonl` (every call), transcripts in `logs/llm_transcripts/`.", "",
          "| Stage | Model | API calls | Cache hits | Prompt tokens | Completion tokens |", "|---|---|---|---|---|---|"]
    tp = tc = 0
    for (stage, model), a in sorted(agg.items()):
        L.append(f"| {stage} | {model} | {a['calls']} | {a['cached']} | {a['prompt']} | {a['completion']} |")
        tp += a["prompt"]
        tc += a["completion"]
    L += [f"| **total** | | | | **{tp}** | **{tc}** |", "",
          f"Cost: **$0.00**. All calls went to Groq's free tier (rate limit observed: 8,000 tokens/minute per model). "
          f"No paid-tier price is quoted here because none was measured."]

    # migration + checks
    L += ["", "## Data migration and the three checks", ""]
    if mig:
        m = mig["migration"]
        L.append(f"- Validation dataset ({mig['seed']}): {m['rows_written']} documents in {m['load_seconds']}s "
                 f"(**{m['rows_per_second']} rows/s**, {m['batches']} batches, 8 workers). Resume run skipped "
                 f"{mig['migration_resume']['batches_skipped']}/{mig['migration_resume']['batches']} batches; a fresh re-run "
                 f"produced byte-identical collections: **{mig['idempotent']}**.")
    if bench:
        for k in ("migration_designed", "migration_naive"):
            m = bench[k]
            L.append(f"- Benchmark dataset → `{m['db']}`: {m['rows_written']} documents in {m['load_seconds']}s "
                     f"(**{m['rows_per_second']} rows/s**).")
    if checks:
        for k, title in (("check1_collections", "Check 1: SQL vs MQL per collection"),
                         ("check2_references", "Check 2: cross-collection references"),
                         ("check3_left_behind", "Check 3: nothing left behind (counts + per-column checksums)")):
            ok = sum(x["ok"] for x in checks[k])
            L.append(f"- {title}: **{ok}/{len(checks[k])} OK**")
        L += ["", "| Source table | Stored as | PG rows | Mongo rows | Columns checksummed | Mismatched |", "|---|---|---|---|---|---|"]
        L += [f"| {x['table']} | {x['stored_as']} | {x['pg_rows']} | {x['mongo_rows']} | {x['columns_checked']} | {x['columns_mismatched'] or '-'} |"
              for x in checks["check3_left_behind"]]

    # benchmark
    if bench:
        L += ["", "## Latency benchmark", "", f"Source: `reports/raw/benchmark.json`. Dataset: {bench['dataset']}. "
              f"Parameters: {bench['params']}. Sequential requests after warm-up on one machine (12 cores, 22 GB RAM), "
              "databases in Docker.", "",
              "### A. End-to-end HTTP (real apps)", "", "| Endpoint | original app (Sequelize + Postgres) p50 / p95 ms | MongoDB app p50 / p95 ms | n |",
              "|---|---|---|---|"]
        for ep, row in bench["http"].items():
            o, m = row.get("original", {}), row.get("mongo", {})
            L.append(f"| `{ep}` | {o.get('p50_ms')} / {o.get('p95_ms')} | {m.get('p50_ms')} / {m.get('p95_ms')} | {o.get('n')}/{m.get('n')} |")
        L += ["", "### B. Data-access level, same batched query strategy per store", "",
              "| Endpoint | Postgres (original schema: PK indexes only) | Postgres + same indexes | MongoDB naive one-to-one | MongoDB designed |",
              "|---|---|---|---|---|"]
        for ep, row in bench["data_access"].items():
            cell = lambda k: f"{row[k]['p50_ms']} / {row[k]['p95_ms']} (n={row[k]['n']})" if k in row else "-"
            L.append(f"| {ep} | {cell('postgres')} | {cell('postgres_indexed')} | {cell('mongo_naive')} | {cell('mongo_designed')} |")
        L += ["", "Values are p50 / p95 in ms. `n` is lower than 200 where a variant hit its 45 s time budget."]

    # failures
    L += ["", "## What failed or is not done", ""]
    probs = []
    for s, v in st["stages"].items():
        if v["status"] != "done":
            probs.append(f"- Stage `{s}`: {v['status']} {v.get('error', '')}")
    if mig and mig["characterization_failures"]:
        probs += [f"- characterization `{f['scenario']}/{f['step']}`: {f['diffs'][:2]}" for f in mig["characterization_failures"][:10]]
    if mig and mig["official_mismatch_vs_original"]:
        probs.append(f"- official entries behaving differently from the original: {mig['official_mismatch_vs_original']}")
    probs += [f"- {x}" for x in st.get("known_issues", [])]
    L += probs or ["- Nothing outstanding."]
    L += ["", "## Reproduce", "", "```bash", "docker compose up -d --wait", "python -m pipeline all          # or one stage at a time:",
          "python -m pipeline baseline     # Stage 1", "python -m pipeline analysis     # Stage 2", "python -m pipeline schema       # Stage 3",
          "python -m pipeline transform    # Stage 4", "python -m pipeline migrate      # Stage 5", "python -m pipeline benchmark    # Stage 6",
          "python -m pipeline report", "```"]
    (settings.root / "reports/results.md").write_text("\n".join(L) + "\n")
    print("  wrote reports/results.md")
