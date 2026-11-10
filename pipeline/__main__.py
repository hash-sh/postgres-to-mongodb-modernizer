"""Orchestrator CLI - a plain, explicit state machine.

    python -m pipeline status
    python -m pipeline baseline | analysis | schema | transform | migrate | benchmark | report
    python -m pipeline all          # runs every stage not yet "done", in order; resumes after a crash
    python -m pipeline reset <stage> # mark a stage (and everything after it) pending again
"""
import importlib
import sys
import time
import traceback

from pipeline.llm import BudgetExhausted
from pipeline.state import STAGES, State


def run_stage(state, name, **kw):
    print(f"\n=== Stage: {name} ===")
    state.set_stage(name, "running")
    t0 = time.time()
    try:
        importlib.import_module(f"pipeline.stages.{name}").run(state, **kw)
    except BudgetExhausted as e:
        state.set_stage(name, "paused", error=str(e))
        print(f"\n[paused] LLM budget/rate limit reached: {e}\nProgress is saved. Re-run the same command to resume.")
        sys.exit(3)
    except Exception as e:
        state.set_stage(name, "failed", error=f"{type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
    st = state.stage(name)
    if st["status"] == "running":
        state.set_stage(name, "done")
    state.set_stage(name, state.stage(name)["status"], seconds=round(time.time() - t0, 1))
    print(f"=== {name}: {state.stage(name)['status']} ({time.time() - t0:.1f}s)")


def main(argv):
    state = State()
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        for s in STAGES:
            st = state.stage(s)
            print(f"{s:10s} {st['status']:8s} {st.get('error', '')}")
        for c, v in state.data["chunks"].items():
            print(f"  chunk {c:28s} {v['status']:8s} iterations={len(v.get('iterations', []))}")
        print(f"LLM: {state.data['llm']}")
    elif cmd == "all":
        for s in STAGES:
            if state.stage(s)["status"] != "done":
                run_stage(state, s)
    elif cmd == "reset":
        idx = STAGES.index(argv[1])
        for s in STAGES[idx:]:
            state.set_stage(s, "pending")
    elif cmd in STAGES:
        kw = {}
        if "--refreeze" in argv:
            kw["refreeze"] = True
        run_stage(state, cmd, **kw)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
