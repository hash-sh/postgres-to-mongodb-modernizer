"""Run request scenarios against a backend, normalize responses, compare them.

Pure, deterministic code. Used by:
  * characterization tests (golden master recorded from the ORIGINAL app)
  * the differential test (same request sequence against both backends)

Normalization rules (each one is deliberate and documented in DESIGN.md):
  * "token" values         -> "<token>"   (JWT contains `iat`, differs every run)
  * ISO-8601 timestamps    -> "<ts>"      (only in `fresh` mode, i.e. for data created during the run;
                                            the differential test keeps migrated timestamps STRICT)
  * "tags" and "tagList"   -> sorted      (the original reads them with no ORDER BY, so their order
                                            is unspecified by SQL; the analyzer flags this)
  * HTTP 500 error text    -> "<internal error>"  (the message is a leaked JS exception string,
                                            e.g. "Cannot read properties of null"; status code is kept)
"""
import copy
import re
import time

import requests

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
UNORDERED_KEYS = {"tags", "tagList"}


def normalize(body, status, ts_cutoff=None):
    """ts_cutoff=None: replace every timestamp. Else only timestamps >= cutoff (ISO string compare)."""
    b = copy.deepcopy(body)

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "token" and isinstance(v, str) and v:
                    x[k] = "<token>"
                elif isinstance(v, str) and ISO.match(v) and (ts_cutoff is None or v >= ts_cutoff):
                    x[k] = "<ts>"
                elif k in UNORDERED_KEYS and isinstance(v, list) and all(isinstance(i, str) for i in v):
                    x[k] = sorted(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(b)
    if status >= 500 and isinstance(b, dict) and "errors" in b:
        b["errors"] = {"body": ["<internal error>"]}
    return b


def _get(obj, dotted):
    for part in dotted.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def run_scenario(base_url, scenario, ts_cutoff=None, timeout=15):
    """Execute one scenario. Returns a list of {step, status, body} (normalized)."""
    tokens, vars_ = {}, {}
    out = []
    s = requests.Session()
    for step in scenario["steps"]:
        path = step["path"].format(**vars_)
        headers = {}
        if step.get("as"):
            headers["Authorization"] = f"Token {tokens[step['as']]}"
        if "auth_header" in step:
            headers["Authorization"] = step["auth_header"]
        try:
            r = s.request(step["method"], base_url + path, json=step.get("body"), headers=headers, timeout=timeout)
            status = r.status_code
            try:
                body = r.json()
            except ValueError:
                body = {"<non-json>": r.text[:200]}
        except requests.RequestException as e:
            status, body = 0, {"<connection error>": type(e).__name__}
        if step.get("save_token") and status < 300 and isinstance(body, dict):
            tokens[step["save_token"]] = _get(body, "user.token")
        for var, dotted in step.get("capture", {}).items():
            try:
                vars_[var] = _get(body, dotted)
            except (KeyError, IndexError, TypeError):
                vars_[var] = "__missing__"
        out.append({"step": step["name"], "request": f"{step['method']} {path}", "status": status,
                    "body": normalize(body, status, ts_cutoff)})
    return out


def run_all(base_url, scenarios, reset=None, ts_cutoff=None):
    """Run scenarios; `reset()` is called before each one so scenarios are independent."""
    results = {}
    for sc in scenarios:
        if reset:
            reset()
        results[sc["name"]] = run_scenario(base_url, sc, ts_cutoff)
    return results


def diff(expected, actual, path="$"):
    """Return a list of human-readable differences between two JSON values (deterministic order)."""
    out = []
    if type(expected) is not type(actual):
        return [f"{path}: expected {_short(expected)} got {_short(actual)}"]
    if isinstance(expected, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in actual:
                out.append(f"{path}.{k}: missing (expected {_short(expected[k])})")
            elif k not in expected:
                out.append(f"{path}.{k}: unexpected key (got {_short(actual[k])})")
            else:
                out += diff(expected[k], actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            out.append(f"{path}: expected list of {len(expected)} got {len(actual)}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            out += diff(e, a, f"{path}[{i}]")
    elif expected != actual:
        out.append(f"{path}: expected {_short(expected)} got {_short(actual)}")
    return out


def _short(v, n=120):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def compare(golden, results):
    """Compare result sets step by step. Returns (passed, total, failures[])."""
    passed = total = 0
    failures = []
    for name, steps in results.items():
        exp_steps = golden[name]
        for i, exp in enumerate(exp_steps):
            total += 1
            act = steps[i] if i < len(steps) else {"status": None, "body": None}
            d = ([f"status: expected {exp['status']} got {act['status']}"] if exp["status"] != act["status"] else []) \
                + diff(exp["body"], act["body"], "$body")
            if d:
                failures.append({"scenario": name, "step": exp["step"], "request": exp["request"], "diffs": d[:12]})
            else:
                passed += 1
    return passed, total, failures


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
