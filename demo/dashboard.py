"""mini-AMP pipeline dashboard (Streamlit).

Reads ONLY the artifacts that real runs wrote (state/, reports/raw/, logs/). Nothing is typed in by hand.
Run:  .venv/bin/streamlit run demo/dashboard.py      (also deployable as-is on Streamlit Community Cloud)
"""
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "reports/raw"


def load(p):
    p = ROOT / p
    return json.loads(p.read_text()) if p.exists() else None


state = load("state/pipeline_state.json") or {}
base = load("reports/raw/official_original.json")
mig = load("reports/raw/migrate_stage.json")
checks = load("reports/raw/checks.json")
bench = load("reports/raw/benchmark.json")
diff = load("reports/raw/differential.json")

st.set_page_config(page_title="mini-AMP dashboard", layout="wide")
st.title("mini-AMP: Postgres → MongoDB application modernizer")
st.caption("Conduit (Express + Sequelize + Postgres) → Mongoose + MongoDB. Deterministic pipeline, LLM agents only "
           "where judgment is needed. Every number below is read from an artifact of an actual run.")

# ------------------------------------------------------------------ headline
c = st.columns(5)
if mig:
    c[0].metric("Characterization tests", mig["characterization"], help="155 steps recorded from the ORIGINAL app, hash-frozen")
    c[1].metric("Official suite (Mongo)", mig["official_mongo"], f"original {base['passed']}/{base['total']}" if base else None)
    d = mig["differential"]
    c[2].metric("Differential replay", f"{d['matched']}/{d['requests']}", "identical responses")
if checks:
    ok = sum(x["ok"] for k in ("check1_collections", "check2_references", "check3_left_behind") for x in checks[k])
    tot = sum(len(checks[k]) for k in ("check1_collections", "check2_references", "check3_left_behind"))
    c[3].metric("Migration checks", f"{ok}/{tot}", "SQL=MQL · refs · nothing lost")
if bench:
    c[4].metric("Migration throughput", f"{bench['migration_designed']['rows_per_second']:,.0f} rows/s",
                f"{bench['migration_designed']['rows_written']:,} docs")

tabs = st.tabs(["Pipeline", "Tests", "Migration checks", "Benchmark", "Agent repair log", "LLM usage", "Known issues"])

# ------------------------------------------------------------------ pipeline
with tabs[0]:
    st.subheader("Stages (state/pipeline_state.json)")
    kind = {"baseline": "deterministic", "analysis": "deterministic (Babel AST)", "schema": "LLM + deterministic lint + review gate",
            "transform": "LLM agent + repair loop", "migrate": "deterministic", "benchmark": "deterministic", "report": "deterministic"}
    st.dataframe(pd.DataFrame([{"stage": s, "kind": kind.get(s, ""), "status": v.get("status"), "seconds": v.get("seconds")}
                               for s, v in state.get("stages", {}).items()]), hide_index=True, width='stretch')
    st.markdown("""
```
deterministic ───────────── LLM (judgment) ───────────── deterministic
1 baseline + freeze tests    3 schema design (DSL)         3 lint gate
2 parser inventory           4 code transformation         4 frozen tests + route contract
                               repair loop                 5 migrate + 3 checks + differential
                               optimization pass           6 benchmark + report
```""")
    sch = load("design/schema.json")
    if sch:
        st.subheader("Designed MongoDB schema (design/schema.json)")
        st.dataframe(pd.DataFrame([{
            "collection": x["name"], "from table": x["source"], "_id": "+".join(x["id"]),
            "embedded": ", ".join(a["name"] for a in x.get("embedArrays", [])) or "-",
            "counters": ", ".join(k["name"] for k in x.get("counters", [])) or "-",
            "indexes": "; ".join(json.dumps(i["keys"]) for i in x.get("indexes", [])) or "-"} for x in sch["collections"]]),
            hide_index=True, width='stretch')

# ------------------------------------------------------------------ tests
with tabs[1]:
    if base and mig:
        st.subheader("Official RealWorld Hurl suite, per file")
        orig = base["per_file"]
        st.dataframe(pd.DataFrame([{"file": f, "original passed": v["passed"], "entries": v["total"]} for f, v in orig.items()]),
                     hide_index=True)
        st.write("Entries whose result differs between the original and MongoDB:",
                 mig["official_mismatch_vs_original"] or "none")
    if diff:
        st.subheader(f"Differential replay: {diff['matched']}/{diff['requests']} identical")
        st.caption("The same request sequence on both backends over identical migrated data; only tokens and timestamps "
                   "of rows created during the run are normalized. Migrated timestamps are compared strictly.")
        st.dataframe(pd.DataFrame([{"request": f"{s['method']} {s['path']}", "step": s["name"]} for s in diff["sequence"]["steps"]]),
                     hide_index=True, height=300, width='stretch')
        if diff["mismatches"]:
            st.write(diff["mismatches"])

# ------------------------------------------------------------------ checks
with tabs[2]:
    if checks:
        st.subheader("Check 1: SQL query vs MQL query per collection")
        st.dataframe(pd.DataFrame(checks["check1_collections"]).drop(columns=["examples"]), hide_index=True)
        st.subheader("Check 2: every cross-collection reference resolves")
        st.dataframe(pd.DataFrame(checks["check2_references"]), hide_index=True)
        st.subheader("Check 3: nothing left behind (row counts + per-column SHA-256 checksums)")
        st.dataframe(pd.DataFrame(checks["check3_left_behind"]).drop(columns=["checksums"]), hide_index=True)

# ------------------------------------------------------------------ benchmark
with tabs[3]:
    if bench:
        st.caption(f"Dataset: {bench['dataset']}")
        st.subheader("Data-access latency p50 (ms), same query strategy per store")
        da = pd.DataFrame({ep: {k: v["p50_ms"] for k, v in row.items()} for ep, row in bench["data_access"].items()}).T
        st.bar_chart(da, horizontal=True, stack=False, height=420)
        st.dataframe(da, width='stretch')
        st.subheader("End-to-end HTTP p50 (ms): original app vs MongoDB app")
        http = pd.DataFrame({ep: {k: v["p50_ms"] for k, v in row.items()} for ep, row in bench["http"].items()}).T
        st.bar_chart(http, horizontal=True, stack=False, height=380)
        st.dataframe(http, width='stretch')
        st.info("Postgres WITH the same indexes beats the designed MongoDB schema on single article + comments and on "
                "profile. Most of the gap vs the original comes from indexing. Shown on purpose.")

# ------------------------------------------------------------------ repair log
with tabs[4]:
    st.subheader("Repair iterations per chunk")
    rows = []
    for cid, cv in state.get("chunks", {}).items():
        for it in cv.get("iterations", []):
            rows.append({"chunk": cid, "round": it.get("round"), "iteration": it.get("iteration"), "characterization": it.get("char"),
                         "official mismatches": it.get("official_mismatch"), "repaired": it.get("repaired")})
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, width='stretch', height=360)
    st.subheader("Root causes diagnosed when the repair cap was hit")
    for cid, cv in state.get("chunks", {}).items():
        for d in cv.get("diagnoses", []):
            with st.expander(f"{cid}: {d['root_cause']}"):
                st.write("**Findings:**", d["findings"])
                st.write("**Fix:**", d["fix"])

# ------------------------------------------------------------------ llm
with tabs[5]:
    p = ROOT / "logs/llm_calls.jsonl"
    if p.exists():
        agg = defaultdict(lambda: {"calls": 0, "cache hits": 0, "prompt tokens": 0, "completion tokens": 0})
        for line in p.read_text().splitlines():
            r = json.loads(line)
            a = agg[(r["purpose"].split(".")[0], r["model"])]
            if r["cache_hit"]:
                a["cache hits"] += 1
            else:
                a["calls"] += 1
                a["prompt tokens"] += r["prompt_tokens"] or 0
                a["completion tokens"] += r["completion_tokens"] or 0
        st.dataframe(pd.DataFrame([{"stage": k[0], "model": k[1], **v} for k, v in agg.items()]), hide_index=True)
        st.caption("Groq free tier: $0. Daily limits reached: " + str(state.get("llm", {}).get("daily_exhausted", {})))

# ------------------------------------------------------------------ known issues
with tabs[6]:
    for k, v in state.get("accepted_deviations", {}).items():
        st.warning(f"**Accepted deviation `{k}`**: {v}")
    for x in state.get("known_issues", []):
        st.write("•", x)
