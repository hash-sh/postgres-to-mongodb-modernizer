"""Anti-cheating guard: "the agent must not grade its own homework".

Three mechanisms, all deterministic code (no LLM involved):

1. Test-suite freeze.  At Stage 1 every file under tests/official and
   tests/characterization is hashed (SHA-256) into tests/MANIFEST.sha256 and the
   manifest's own hash is stored in the pipeline state. `verify_tests()` runs
   before EVERY test run and raises if anything changed, was added or removed.

2. Path allowlist.  The transformation agent never touches the filesystem
   directly. It returns file contents; `SafeWriter.write()` only accepts paths
   inside apps/mongo-backend that match DATA_LAYER_ALLOWLIST. Anything else
   (tests, fixtures, routes, error handler, package.json, ...) is rejected.

3. API-contract check.  The set of (METHOD, path) routes in the generated app,
   extracted by the same Babel analyzer, must equal the original's.
"""
import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path

from pipeline.config import settings

FROZEN_DIRS = ["tests/official", "tests/characterization"]
MANIFEST = settings.root / "tests/MANIFEST.sha256"
# Tests added AFTER Stage 1 (e.g. to cover a gap found in review) are recorded against the ORIGINAL app and frozen
# in their OWN manifest, so the Stage-1 manifest is never rewritten.
ADDENDUM_DIRS = ["tests/characterization_addendum"]
ADDENDUM_MANIFEST = settings.root / "tests/MANIFEST_addendum.sha256"

# The ONLY files the transformation agent may write (relative to apps/mongo-backend).
# These are exactly the files the analyzer found data-access call sites in, plus the
# new Mongo connection module. routes/*.js other than tags.js, helper/customErrors.js,
# middleware/errorHandler.js, helper/jwt.js, helper/bcrypt.js are NOT writable: they
# define the API contract (paths, status codes, error shapes, token format).
DATA_LAYER_ALLOWLIST = [
    "index.js",
    "db/*.js",
    "models/*.js",
    "controllers/*.js",
    "helper/helpers.js",
    "middleware/authentication.js",
    "routes/tags.js",
]


class GuardError(RuntimeError):
    pass


def _files(dirs=None):
    out = []
    for d in (dirs or FROZEN_DIRS):
        for p in sorted((settings.root / d).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p)
    return out


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest_text(dirs=None) -> str:
    return "".join(f"{_sha(p)}  {p.relative_to(settings.root)}\n" for p in _files(dirs))


def freeze(dirs=None, manifest=None) -> str:
    """Write the manifest; return its hash (store it in pipeline state)."""
    manifest = manifest or MANIFEST
    text = manifest_text(dirs)
    manifest.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def verify_tests(expected_manifest_hash: str, dirs=None, manifest=None):
    """Fail loudly if any frozen test file changed since it was frozen."""
    MANIFEST = manifest or globals()["MANIFEST"]
    if not MANIFEST.exists():
        raise GuardError("tests/MANIFEST.sha256 missing - Stage 1 (baseline) must freeze the test suite first")
    stored = MANIFEST.read_text()
    if hashlib.sha256(stored.encode()).hexdigest() != expected_manifest_hash:
        raise GuardError("TEST MANIFEST WAS MODIFIED after it was frozen (hash differs from pipeline state)")
    current = manifest_text(dirs)
    if current != stored:
        a, b = set(stored.splitlines()), set(current.splitlines())
        raise GuardError("FROZEN TEST SUITE CHANGED:\n  removed/changed: " + "\n    ".join(sorted(a - b))
                         + "\n  added/changed:   " + "\n    ".join(sorted(b - a)))


class SafeWriter:
    """The only way agent output reaches disk."""

    def __init__(self, base: Path = None, allowlist=DATA_LAYER_ALLOWLIST):
        self.base = (base or settings.mongo_backend).resolve()
        self.allowlist = allowlist
        self.written = []

    def check(self, rel: str) -> Path:
        rel = rel.strip().lstrip("./") if rel.startswith("./") else rel.strip()
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise GuardError(f"agent tried to write outside the app: {rel!r}")
        target = (self.base / rel).resolve()
        if self.base not in target.parents:
            raise GuardError(f"agent tried to write outside the app: {rel!r}")
        if target.is_symlink():
            raise GuardError(f"refusing to write through a symlink: {rel!r}")
        r = target.relative_to(self.base).as_posix()
        if not any(fnmatch.fnmatchcase(r, pat) for pat in self.allowlist):
            raise GuardError(f"path not in data-layer allowlist: {r!r} (allowed: {self.allowlist})")
        return target

    def write(self, rel: str, content: str) -> Path:
        target = self.check(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.written.append(str(target.relative_to(self.base)))
        return target


def routes_of(backend: Path):
    out = subprocess.run(["node", str(settings.root / "analyzer/analyze.js"), str(backend)],
                         capture_output=True, text=True, check=True).stdout
    return sorted({(r["method"], r["path"]) for r in json.loads(out)["routes"]})


def verify_api_contract():
    orig, new = routes_of(settings.original_backend), routes_of(settings.mongo_backend)
    if orig != new:
        raise GuardError(f"API CONTRACT CHANGED. missing={sorted(set(orig) - set(new))} added={sorted(set(new) - set(orig))}")
    return len(orig)
