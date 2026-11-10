"""Run the official RealWorld API suite (Hurl files from gothinkster/realworld) via the Hurl Docker image.

We count results per REQUEST ENTRY (an entry passes if all its asserts pass), with
--continue-on-error so one failure does not hide the rest of a file. That gives a
finer and more honest number than "files passed".
"""
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from pipeline.config import settings

HURL_IMAGE = "ghcr.io/orange-opensource/hurl@sha256:f8aed33658ba793ed53c5a5b2e57cadd9ec9322c8b56c550652c1f4cbaa241c5"  # hurl 8.1.0
SUITE = settings.root / "tests/official/hurl"

# Which conversion slice each official file needs (it may also use endpoints of earlier slices).
FILE_SLICE = {
    "auth.hurl": 1, "errors_auth.hurl": 1, "profiles.hurl": 1, "errors_profiles.hurl": 1,
    "articles.hurl": 2, "errors_articles.hurl": 2, "feed.hurl": 2, "pagination.hurl": 2, "errors_authorization.hurl": 3,
    "comments.hurl": 3, "errors_comments.hurl": 3, "favorites.hurl": 3, "tags.hurl": 3,
}


def run(host: str, max_slice: int = 3, uid: str = None) -> dict:
    files = sorted(f.name for f in SUITE.glob("*.hurl") if FILE_SLICE.get(f.name, 3) <= max_slice)
    uid = uid or uuid.uuid4().hex[:10]
    out_dir = Path(tempfile.mkdtemp(prefix="hurl_"))
    try:
        cmd = ["docker", "run", "--rm", "--network", "host", "-u", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{SUITE}:/t:ro", "-v", f"{out_dir}:/out", HURL_IMAGE,
               "--test", "--jobs", "1", "--continue-on-error",
               "--variable", f"host={host}", "--variable", f"uid={uid}",
               "--report-json", "/out/r", *[f"/t/{f}" for f in files]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        report = json.loads((out_dir / "r/report.json").read_text())
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    per_file, entries = {}, {}
    for f in report:
        name = Path(f["filename"]).name
        ok = [all(a.get("success") for a in e.get("asserts", [])) for e in f["entries"]]
        per_file[name] = {"passed": sum(ok), "total": len(ok)}
        for e, good in zip(f["entries"], ok):
            entries[f"{name}:{e['line']}"] = good
    return {
        "passed": sum(v["passed"] for v in per_file.values()),
        "total": sum(v["total"] for v in per_file.values()),
        "files_passed": sum(1 for v in per_file.values() if v["passed"] == v["total"]),
        "files": len(per_file), "per_file": per_file, "entries": entries,
        "failure_excerpt": proc.stderr[-3000:],
    }
