"""The schema DSL, its deterministic lint, and the naive one-to-one baseline.

The LLM designs the schema, but it must express it in this small DSL. Everything
downstream (Mongoose model codegen, the migrator, the 3 validation checks) reads the
same file, so the design can be *executed and verified*, not just admired.

schema.json = {"collections": [ {
    "name": "articles",                 target collection
    "source": "Articles",               one source row  ->  one document
    "id": ["id"],                       source PK column(s) -> _id (1 col: scalar, >1: {col: val, ...})
    "fields": [{"name": "slug", "from": "slug", "type": "string"}],      column -> field
    "embedArrays": [{"name": "tagList", "fromTable": "TagList", "parentKey": "articleId",
                     "valueColumn": "tagName", "maxItems": 20, "reason": "..."}],   child rows -> array of scalars
    "extendedRefs": [{"name": "author", "fromTable": "Users", "localKey": "userId",
                      "copy": ["username"], "reason": "..."}],             FK -> {id: fk, ...copied parent columns}
    "counters": [{"name": "favoritesCount", "fromTable": "Favorites", "parentKey": "articleId", "reason": "..."}],
    "indexes": [{"keys": {"slug": 1}, "unique": true}]
}]}
"""
import json

import psycopg

from pipeline.config import settings

DSL_SPEC = __doc__.split("schema.json =", 1)[1]
PG_TYPE = {"integer": "int", "bigint": "long", "text": "string", "timestamp with time zone": "date", "boolean": "bool"}
MAX_DOC_BYTES_ERROR = 16 * 1024 * 1024
MAX_DOC_BYTES_TARGET = 1024 * 1024


def mongo_type(pg_type: str) -> str:
    return "string" if pg_type.startswith("character varying") else PG_TYPE.get(pg_type, "string")


# ---------------------------------------------------------------------- query patterns (from the inventory)
def required_query_patterns(inv):
    """Every predicate/sort the app issues, at SOURCE level: (table, [columns], sortColumn|None, why).

    Derived deterministically from the analyzer's call sites."""
    T = {"User": "Users", "Article": "Articles", "Comment": "Comments", "Tag": "Tags"}
    pats = set()
    for s in inv["callSites"]:
        o = s.get("options") if isinstance(s.get("options"), dict) else {}
        if s.get("model") and isinstance(o.get("where"), dict):
            cols = sorted(k for k, v in o["where"].items() if not (isinstance(v, dict) and "$conditional" in v))
            order = o.get("order")
            sort = order[0][0] if isinstance(order, list) and order and isinstance(order[0], list) else None
            if cols:
                pats.add((T[s["model"]], tuple(cols), sort, f"{s['file']}:{s['line']} {s['method']}"))
        if s.get("model") and o.get("order") and not o.get("where"):
            pats.add((T[s["model"]], (), o["order"][0][0], f"{s['file']}:{s['line']} {s['method']} order"))
        for inc in o.get("include", []) if isinstance(o.get("include"), list) else []:
            w = inc.get("where", {}) if isinstance(inc, dict) else {}
            w = w.get("$conditional", w) if isinstance(w, dict) else {}
            if isinstance(w, dict) and w:
                tbl = "TagList" if inc.get("as") == "tagList" else T.get(inc["model"]["$id"])
                cols = ("tagName",) if tbl == "TagList" else tuple(sorted(w))
                pats.add((tbl, cols, None, f"{s['file']}:{s['line']} include filter"))
    mixin = {  # association access -> junction predicates
        "User.followers": [("Followers", ("followerId", "userId")), ("Followers", ("userId",))],
        "User.following": [("Followers", ("followerId",))],
        "Article.User": [("Favorites", ("articleId", "userId")), ("Favorites", ("articleId",))],
        "User.favorites": [("Favorites", ("userId",))],
        "Article.tagList": [("TagList", ("articleId",))],
        "Article.Comment": [("Comments", ("articleId",))],
    }
    for s in inv["callSites"]:
        if s["kind"] == "association":
            for c in s["candidates"]:
                for tbl, cols in mixin.get(c, []):
                    pats.add((tbl, cols, None, f"{s['file']}:{s['line']} {s['method']}"))
    # The feed filters articles by author and sorts by date (where: {userId: [...]}, order createdAt) - from the
    # analyzer's options on articlesFeed; author filter uses the same index.
    merged = {}
    for t, c, srt, why in sorted(pats):
        merged.setdefault((t, c, srt), []).append(why)
    return [{"table": t, "columns": list(c), "sort": srt, "usedBy": w} for (t, c, srt), w in sorted(merged.items())]


# ---------------------------------------------------------------------- data profile (for size + bounds)
def data_profile():
    """Row-size and fan-out statistics from Postgres, used by the bounded-array and 16 MB rules."""
    prof = {"maxRowBytes": {}, "fanout": {}}
    with psycopg.connect(settings.pg_dsn) as c:
        for t in ["Users", "Articles", "Comments", "Tags", "TagList", "Favorites", "Followers"]:
            prof["maxRowBytes"][t] = c.execute(f'SELECT COALESCE(MAX(pg_column_size(x.*)),0) FROM "{t}" x').fetchone()[0]
        for t, k in [("TagList", "articleId"), ("Favorites", "articleId"), ("Favorites", "userId"),
                     ("Followers", "userId"), ("Followers", "followerId"), ("Comments", "articleId"), ("Articles", "userId")]:
            mx, avg = c.execute(f'SELECT COALESCE(MAX(n),0), COALESCE(AVG(n),0) FROM (SELECT count(*) n FROM "{t}" GROUP BY "{k}") s').fetchone()
            prof["fanout"][f"{t}.{k}"] = {"max": mx, "avg": round(float(avg), 2)}
    return prof


# ---------------------------------------------------------------------- lint
def lint(schema, inv, profile):
    """Return (errors, warnings). Errors block the pipeline."""
    E, W = [], []
    pg = inv["postgres"]
    cols = {t: set(v["columns"]) for t, v in pg.items()}
    colls = schema.get("collections", [])
    names = [c.get("name") for c in colls]
    if len(set(names)) != len(names):
        E.append("duplicate collection names")

    def fks(table):
        return {fk["column"]: fk["references"].split(".")[0] for fk in pg[table]["foreignKeys"]}

    def is_catalog(table):  # a lookup table that does not grow with user activity
        return not pg[table]["foreignKeys"] and "createdAt" not in pg[table]["columns"]

    covered = {t: set() for t in pg}
    paths = {}  # (table, column) -> (collection, field path)
    for c in colls:
        n, src = c.get("name"), c.get("source")
        if src not in pg:
            E.append(f"{n}: unknown source table {src!r}")
            continue
        ids = c.get("id") or []
        if sorted(ids) != sorted(pg[src]["primaryKey"]):
            E.append(f"{n}: id {ids} must be exactly the source primary key {pg[src]['primaryKey']} (R9 deterministic _id)")
        for i in ids:
            covered[src].add(i)
            paths[(src, i)] = (n, "_id" if len(ids) == 1 else f"_id.{i}")
        for f in c.get("fields", []):
            if f.get("from") not in cols[src]:
                E.append(f"{n}.{f.get('name')}: column {f.get('from')!r} not in {src}")
                continue
            covered[src].add(f["from"])
            paths[(src, f["from"])] = (n, f["name"])
            want = mongo_type(pg[src]["columns"][f["from"]]["type"])
            if f.get("type") and f["type"] != want:
                W.append(f"{n}.{f['name']}: type {f['type']} but source is {pg[src]['columns'][f['from']]['type']} ({want})")
        for r in c.get("extendedRefs", []):
            ft, lk = r.get("fromTable"), r.get("localKey")
            if ft == src:
                E.append(f"{n}.{r.get('name')}: self-referential extended reference to {src} (R4: recursion risk)")
            if lk not in cols[src] or fks(src).get(lk) != ft:
                E.append(f"{n}.{r.get('name')}: localKey {lk!r} is not a FK from {src} to {ft}")
                continue
            bad = [x for x in r.get("copy", []) if x not in cols.get(ft, set())]
            if bad:
                E.append(f"{n}.{r['name']}: copied columns {bad} not in {ft}")
            if "password" in r.get("copy", []) or "email" in r.get("copy", []):
                E.append(f"{n}.{r['name']}: copies a secret/PII column into another collection")
            covered[src].add(lk)
            paths[(src, lk)] = (n, f"{r['name']}.id")
        for a in c.get("embedArrays", []):
            ft, pk, vc = a.get("fromTable"), a.get("parentKey"), a.get("valueColumn")
            if ft not in pg:
                E.append(f"{n}.{a.get('name')}: unknown table {ft}")
                continue
            f = fks(ft)
            if f.get(pk) != src:
                E.append(f"{n}.{a['name']}: parentKey {pk!r} of {ft} does not reference {src}")
                continue
            if ft == src or list(f.values()).count(src) > 1:
                E.append(f"{n}.{a['name']}: self-referential embedding of {ft} into {src} (R4: infinite recursion)")
            others = [t for col, t in f.items() if col != pk]
            if pg[ft]["columns"].get("id", {}).get("default", "").startswith("nextval") or any(not is_catalog(t) for t in others):
                E.append(f"{n}.{a['name']}: {ft} rows are created by user activity => UNBOUNDED array (R2). "
                         f"Use a separate collection (+ counter on the parent if needed).")
            mx = a.get("maxItems")
            fan = profile["fanout"].get(f"{ft}.{pk}", {}).get("max", 0)
            if not isinstance(mx, int) or mx <= 0:
                E.append(f"{n}.{a['name']}: embedded array needs an integer maxItems (R2)")
            elif fan > mx:
                E.append(f"{n}.{a['name']}: data already has {fan} items for one parent > maxItems {mx}")
            covered[ft].update({pk, vc})
            paths[(ft, pk)] = (n, "_id")
            paths[(ft, vc)] = (n, a["name"])
            if set(cols[ft]) - {pk, vc}:
                E.append(f"{n}.{a['name']}: columns {set(cols[ft]) - {pk, vc}} of {ft} would be lost")
        for k in c.get("counters", []):
            ft, pk = k.get("fromTable"), k.get("parentKey")
            if ft not in pg or fks(ft).get(pk) != src:
                E.append(f"{n}.{k.get('name')}: counter source {ft}.{pk} does not reference {src}")
        # 16 MB rule: worst case = biggest source row + every embedded array at maxItems + copied parent fields
        est = profile["maxRowBytes"].get(src, 0) * 2
        for a in c.get("embedArrays", []):
            est += (a.get("maxItems") or 0) * (profile["maxRowBytes"].get(a.get("fromTable"), 64) * 2)
        for r in c.get("extendedRefs", []):
            est += profile["maxRowBytes"].get(r.get("fromTable"), 0) * 2
        c["_estimatedMaxDocBytes"] = est
        if est >= MAX_DOC_BYTES_ERROR:
            E.append(f"{n}: estimated max document size {est} B exceeds the 16 MB BSON limit (R7)")
        elif est >= MAX_DOC_BYTES_TARGET:
            W.append(f"{n}: estimated max document size {est} B above the 1 MB target")

    # A source column stored twice (e.g. as a field AND as an extended-reference id) must be kept consistent twice
    for c in colls:
        srcs = [f.get("from") for f in c.get("fields", [])] + [r.get("localKey") for r in c.get("extendedRefs", [])]
        for d in sorted({x for x in srcs if srcs.count(x) > 1}):
            W.append(f"{c.get('name')}: source column {d!r} is stored twice")

    # Index keys must name fields that exist (catches redundant indexes on renamed/moved columns)
    for c in colls:
        known = {"_id"} | {f"_id.{i}" for i in c.get("id", [])} | {f.get("name") for f in c.get("fields", [])}
        known |= {a.get("name") for a in c.get("embedArrays", [])} | {k.get("name") for k in c.get("counters", [])}
        for r in c.get("extendedRefs", []):
            known |= {f"{r.get('name')}.id"} | {f"{r.get('name')}.{x}" for x in r.get("copy", [])}
        for i in c.get("indexes", []):
            bad = [k for k in i.get("keys", {}) if k not in known]
            if bad:
                E.append(f"{c.get('name')}: index {i.get('keys')} uses unknown fields {bad}")

    # R8 recoverability: every table and column must land somewhere
    for t, cs in cols.items():
        missing = cs - covered[t]
        if missing == cs:
            E.append(f"source table {t} is not recoverable from the design (R8)")
        elif missing:
            E.append(f"columns {sorted(missing)} of {t} are not recoverable (R8)")

    # R10 index coverage
    idx = {c["name"]: [list(i.get("keys", {})) for i in c.get("indexes", [])] for c in colls if "name" in c}
    id_paths = {c["name"]: (["_id"] if len(c.get("id", [])) == 1 else [f"_id.{i}" for i in c.get("id", [])]) for c in colls}
    for p in required_query_patterns(inv):
        mapped = [paths.get((p["table"], col)) for col in p["columns"]]
        if any(m is None for m in mapped):
            continue  # recoverability errors already reported
        coll_names = {m[0] for m in mapped}
        sort = None
        if p["sort"]:
            sp = paths.get((p["table"], p["sort"]))
            sort = sp[1] if sp else None
            coll_names |= {sp[0]} if sp else set()
        if len(coll_names) > 1:
            continue  # cross-collection pattern: each side checked by its own single-table pattern
        coll = coll_names.pop() if coll_names else None
        fields = sorted({m[1] for m in mapped})
        if coll is None:
            continue
        ok = False
        if fields and (fields == ["_id"] or sorted(fields) == sorted(id_paths.get(coll, []))) and not sort:
            ok = True
        for keys in idx.get(coll, []):
            lead = keys[: len(fields)]
            if sorted(lead) == fields and (not sort or (len(keys) > len(fields) and keys[len(fields)] == sort)):
                ok = True
        if not ok:
            E.append(f"no index on {coll} for query {fields}{' sort ' + sort if sort else ''} "
                     f"(source {p['table']}{p['columns']}; used by {p['usedBy'][0]}) (R10)")
    return E, W


# ---------------------------------------------------------------------- naive baseline
def naive_schema(inv):
    """One collection per table, same column names, _id from PK, no embedding, no secondary indexes.

    This is the paper's "one-to-one" baseline: joins become unindexed $lookups."""
    colls = []
    for t, v in inv["postgres"].items():
        pk = v["primaryKey"]
        colls.append({"name": t, "source": t, "id": pk,
                      "fields": [{"name": col, "from": col, "type": mongo_type(m["type"])}
                                 for col, m in v["columns"].items() if col not in pk or len(pk) > 1],
                      "embedArrays": [], "extendedRefs": [], "counters": [], "indexes": []})
    return {"collections": colls}


def compact_inventory(inv, patterns):
    """A token-cheap view of the inventory for the LLM (free tier: ~8k tokens/request)."""
    lines = ["TABLES (column:type, PK, FKs, rows):"]
    for t, v in inv["postgres"].items():
        c = ", ".join(f"{k}:{mongo_type(m['type'])}" for k, m in v["columns"].items())
        f = "; ".join(f"{x['column']}->{x['references']}" for x in v["foreignKeys"])
        lines.append(f"- {t}({c}) PK={v['primaryKey']} FK=[{f}] rows={v['rowCount']}")
    lines.append("ASSOCIATIONS:")
    for a in inv["associations"]:
        lines.append(f"- {a['owner']} {a['type']} {a['target']} as={a.get('as')} through={a.get('through')} [{a['cardinality']}]")
    lines.append("toJSON hides: " + "; ".join(f"{m['name']}:{m['toJSONHidden']}" for m in inv["models"]))
    lines.append("ACCESS PATTERNS (endpoint w=assumed weight: tables read together | written):")
    for p in patterns:
        lines.append(f"- {p['endpoint']} w={p['assumedWeight']}: {','.join(p['readsTogether'])} | {','.join(p['writes']) or '-'}"
                     + (f" (N+1 in original: {len(p['nPlusOneSites'])} per-row queries)" if p["nPlusOneSites"] else ""))
    lines.append("QUERY PREDICATES (source level):")
    for q in required_query_patterns(inv):
        lines.append(f"- {q['table']} where {q['columns']}" + (f" sort {q['sort']} desc" if q["sort"] else ""))
    return "\n".join(lines)


def dump(obj):
    return json.dumps(obj, indent=2)
