"""Persisted pipeline state: a plain JSON file, written atomically after every step.

The orchestrator is an explicit state machine:  STAGES run in a fixed order, each
stage is either "pending", "running", "done" or "failed". Stage 4 additionally
tracks each conversion chunk. If the process crashes (or hits an LLM rate limit),
re-running `python -m pipeline all` skips everything already "done".
"""
import json
import os
import time

from pipeline.config import settings

STAGES = ["baseline", "analysis", "schema", "transform", "migrate", "benchmark", "report"]


class State:
    def __init__(self, path=settings.state_file):
        self.path = path
        self.data = {"stages": {s: {"status": "pending"} for s in STAGES}, "chunks": {}, "llm": {"tokens": 0, "calls": 0}}
        if path.exists():
            self.data = json.loads(path.read_text())
            for s in STAGES:
                self.data["stages"].setdefault(s, {"status": "pending"})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written state file

    def stage(self, name):
        return self.data["stages"][name]

    def set_stage(self, name, status, **extra):
        st = self.data["stages"][name]
        if status in ("running", "done"):
            st.pop("error", None)
        st.update(status=status, updated=time.strftime("%Y-%m-%dT%H:%M:%S"), **extra)
        self.save()

    def chunk(self, cid):
        return self.data["chunks"].setdefault(cid, {"status": "pending", "iterations": []})

    def save_chunk(self, cid, **kv):
        self.chunk(cid).update(kv)
        self.save()
