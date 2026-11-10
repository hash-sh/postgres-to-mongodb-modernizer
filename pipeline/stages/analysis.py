"""Stage 2 - Analysis (deterministic, no LLM).

Inputs : the original backend source + the live Postgres catalog.
Outputs: analysis/inventory.json       models, fields, constraints, associations, routes, call sites
         analysis/access_patterns.json per endpoint: entities read/written together, query features
         analysis/chunks.json          conversion chunks (vertical slices) + their files and tests
"""
import json
import subprocess
from collections import defaultdict

import psycopg

from pipeline.config import settings

OUT = settings.root / "analysis"

# Association alias -> the SQL tables a call through it touches.
ASSOC_TABLES = {
    "Article.author": ["Users"], "Comment.author": ["Users"], "Article.tagList": ["TagList", "Tags"],
    "Article.User": ["Favorites"], "User.favorites": ["Favorites", "Articles"], "User.followers": ["Followers"],
    "User.following": ["Followers", "Users"], "Article.Comment": ["Comments"], "User.Comment": ["Comments"],
}
MODEL_TABLE = {"User": "Users", "Article": "Articles", "Comment": "Comments", "Tag": "Tags"}

# Relative request frequency used to weight access patterns. These are ASSUMPTIONS about a
# Medium-like blog (reads >> writes), not measurements; they only order design priorities.
ASSUMED_WEIGHT = {
    ("GET", "/api/articles"): 30, ("GET", "/api/articles/:slug"): 25, ("GET", "/api/articles/:slug/comments"): 20,
    ("GET", "/api/articles/feed"): 15, ("GET", "/api/profiles/:username"): 8, ("GET", "/api/tags"): 10,
    ("GET", "/api/user"): 10,
}

# Conversion chunks = vertical slices, ordered so each slice only depends on earlier ones.
SLICES = [
    {"id": "slice1_users_profiles", "slice": 1, "prefixes": ["/api/users", "/api/user", "/api/profiles"]},
    {"id": "slice2_articles_feed", "slice": 2, "prefixes": ["/api/articles"], "exclude": ["/comments", "/favorite"]},
    {"id": "slice3_comments_favorites_tags", "slice": 3, "prefixes": ["/api/articles/:slug/comments", "/api/articles/:slug/favorite", "/api/tags"]},
]


def introspect_pg():
    q_cols = """SELECT table_name, column_name, data_type, is_nullable, column_default, character_maximum_length
                FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name, ordinal_position"""
    q_cons = """SELECT tc.table_name, tc.constraint_type, kcu.column_name, ccu.table_name AS ref_table,
                       ccu.column_name AS ref_column, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                LEFT JOIN information_schema.constraint_column_usage ccu
                       ON tc.constraint_name = ccu.constraint_name AND tc.constraint_type = 'FOREIGN KEY'
                LEFT JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_schema='public' ORDER BY tc.table_name, tc.constraint_type, kcu.ordinal_position"""
    tables = defaultdict(lambda: {"columns": {}, "primaryKey": [], "foreignKeys": [], "unique": []})
    with psycopg.connect(settings.pg_dsn) as c:
        for t, col, typ, nullable, default, maxlen in c.execute(q_cols):
            tables[t]["columns"][col] = {"type": typ + (f"({maxlen})" if maxlen else ""), "nullable": nullable == "YES",
                                         **({"default": default} if default else {})}
        for t, ctype, col, ref_t, ref_c, on_delete in c.execute(q_cons):
            if ctype == "PRIMARY KEY":
                tables[t]["primaryKey"].append(col)
            elif ctype == "FOREIGN KEY":
                tables[t]["foreignKeys"].append({"column": col, "references": f"{ref_t}.{ref_c}", "onDelete": on_delete})
            elif ctype == "UNIQUE":
                tables[t]["unique"].append(col)
        for t in tables:
            tables[t]["rowCount"] = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    return dict(sorted(tables.items()))


# Instance calls (`loggedUser.save()`, `article.destroy()`) have no static model; resolve the
# receiver variable by name. Deterministic, but a heuristic - listed in DECISIONS.md.
RECEIVER_MODEL = {"loggedUser": "User", "newUser": "User", "user": "User", "profile": "User", "author": "User",
                  "article": "Article", "comment": "Comment"}


def tables_of(site):
    if not site.get("model") and site["kind"] == "query" and site["receiver"] in RECEIVER_MODEL:
        site["model"] = RECEIVER_MODEL[site["receiver"]]
        site["modelResolvedBy"] = "receiver-name"
    if site.get("model"):
        return [MODEL_TABLE[site["model"]]]
    if site["kind"] == "association":
        return sorted({t for cand in site["candidates"] for t in ASSOC_TABLES.get(cand, [])})
    return []


def include_tables(opts):
    out = []
    if isinstance(opts, dict):
        inc = opts.get("include")
        for i in inc if isinstance(inc, list) else []:
            if isinstance(i, dict) and isinstance(i.get("model"), dict):
                out.append(MODEL_TABLE.get(i["model"]["$id"], i["model"]["$id"]))
                if i.get("as") == "tagList":
                    out.append("TagList")
    return out


def run(state):
    OUT.mkdir(exist_ok=True)
    raw = json.loads(subprocess.run(["node", str(settings.root / "analyzer/analyze.js"), str(settings.original_backend)],
                                    capture_output=True, text=True, check=True).stdout)
    raw["postgres"] = introspect_pg()

    # ---- call graph: route handler -> its call sites + call sites of helpers it calls
    fn_by_name = defaultdict(list)
    for key, f in raw["functions"].items():
        fn_by_name[f["function"]].append(key)
    sites = raw["callSites"]

    loop_sites = set()  # call sites executed once per row (N+1), directly or via a helper called in a loop

    def reachable_sites(fkey, seen=None, in_loop=False):
        seen = set() if seen is None else seen
        if fkey in seen or fkey not in raw["functions"]:
            return []
        seen.add(fkey)
        f = raw["functions"][fkey]
        out = list(f["callSites"])
        for i in out:
            if in_loop or sites[i].get("inLoop"):
                loop_sites.add(i)
        for callee in f["calls"]:
            for k in fn_by_name.get(callee, []):
                out += reachable_sites(k, seen, in_loop or callee in f.get("loopCalls", []))
        return out

    patterns = []
    for r in raw["routes"]:
        if r["handler"]["function"].startswith("<inline GET /") and r["path"] in ("/", "/*any"):
            continue
        loop_sites.clear()  # N+1 is per endpoint
        fkey = f"{r['handler']['file']}#{r['handler']['function']}"
        if fkey not in raw["functions"]:  # inline handlers are keyed by their express call
            fkey = next((k for k in raw["functions"] if k.startswith(r["handler"]["file"] + "#<router.")), fkey)
        idx = reachable_sites(fkey)
        if "verifyToken" in r["middleware"]:
            idx += reachable_sites("middleware/authentication.js#verifyToken")
        idx = sorted(set(idx))
        reads, writes, features = set(), set(), set()
        for i in idx:
            s = sites[i]
            s.setdefault("usedBy", [])
            ep = f"{r['method']} {r['path']}"
            if ep not in s["usedBy"]:
                s["usedBy"].append(ep)
            ts = tables_of(s) + include_tables(s.get("options"))
            (reads if s["op"] == "read" else writes).update(ts)
            features.update(s["features"])
        patterns.append({
            "endpoint": f"{r['method']} {r['path']}", "handler": fkey,
            "readsTogether": sorted(reads), "writes": sorted(writes),
            "queryCount": len(idx), "features": sorted(features),
            "nPlusOneSites": [f"{sites[i]['file']}:{sites[i]['line']} {sites[i]['method']}" for i in idx if i in loop_sites],
            "callSites": [f"{sites[i]['file']}:{sites[i]['line']} {sites[i]['method']}" for i in idx],
            "assumedWeight": ASSUMED_WEIGHT.get((r["method"], r["path"]), 2),
        })
    patterns.sort(key=lambda p: -p["assumedWeight"])

    # ---- chunks
    def slice_of(path):
        best = None
        for s in SLICES:
            for p in s["prefixes"]:
                if (path == p or path.startswith(p + "/")) and not any(x in path for x in s.get("exclude", [])):
                    if best is None or len(p) > len(best[1]):
                        best = (s, p)
        return best[0] if best else None

    assigned = set()
    chunks = []
    for s in SLICES:
        eps = [p for p in patterns if slice_of(p["endpoint"].split(" ", 1)[1]) is s]
        files = []
        for p in eps:
            for cs in p["callSites"]:
                f = cs.split(":")[0]
                if f not in assigned:
                    assigned.add(f)
                    files.append(f)
        if s["slice"] == 1:  # bootstrap files belong to the first slice
            for f in ["index.js"]:
                if f not in assigned:
                    assigned.add(f)
                    files.insert(0, f)
        chunks.append({"id": s["id"], "slice": s["slice"], "endpoints": [p["endpoint"] for p in eps],
                       "files": files})

    inv_summary = {
        "models": len(raw["models"]), "associations": len(raw["associations"]), "routes": len(raw["routes"]),
        "callSites": len(sites), "rawSQL": sum(1 for s in sites if s.get("rawSQL")),
        "transactions": sum(1 for s in sites if s.get("transaction")),
        "selfReferential": [f"{a['owner']}.{a.get('as')}" for a in raw["associations"] if "self" in a["cardinality"]],
        "semanticNotes": len(raw["semanticNotes"]),
    }
    raw["summary"] = inv_summary
    (OUT / "inventory.json").write_text(json.dumps(raw, indent=2))
    (OUT / "access_patterns.json").write_text(json.dumps(patterns, indent=2))
    (OUT / "chunks.json").write_text(json.dumps(chunks, indent=2))
    for c in chunks:
        print(f"  {c['id']}: {len(c['endpoints'])} endpoints, files={c['files']}")
    print(f"  inventory: {inv_summary}")
    state.set_stage("analysis", "done", **{k: v for k, v in inv_summary.items() if isinstance(v, int)})
