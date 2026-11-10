"""The paper's three post-migration checks. Deterministic; no LLM decides anything here.

Check 1  Collection validation: for every collection, a SQL query and an MQL query that reference the same
         columns/fields must return identical results. The SQL side computes embedded arrays (array_agg) and
         counters (count) IN SQL, independently of the migrator's Python code, so a migrator bug is caught.
Check 2  Cross-collection validation: every reference (FK-derived field, compound-_id part, embedded tag name)
         must point to an existing document ("agree under containment": referenced set is a subset of target ids).
Check 3  Nothing left behind: per source table, row count + an order-independent SHA-256 checksum over every
         (primary key, column value) pair, computed separately from Postgres and from MongoDB. Any source value
         without a match is reported as probable data loss.

Usage: python -m pipeline.checks [--db conduit]
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone

import psycopg
from pymongo import MongoClient

from pipeline.config import settings


def q(i):
    return '"' + i + '"'


def canon(v):
    """Canonical text form shared by both sides (timestamps at millisecond precision, UTC)."""
    if v is None:
        return "∅"
    if isinstance(v, datetime):
        if v.tzinfo:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(canon(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{canon(v[k])}" for k in sorted(v)) + "}"
    return str(v)


class Checksum:
    """Order-independent: XOR-free sum of per-item SHA-256 mod 2^256, so row order never matters."""

    def __init__(self):
        self.acc, self.n = 0, 0

    def add(self, *parts):
        self.acc = (self.acc + int.from_bytes(hashlib.sha256("\x1f".join(canon(p) for p in parts).encode()).digest(), "big")) % (1 << 256)
        self.n += 1

    def hex(self):
        return f"{self.acc:064x}"[:24]


def _id_of(coll, row_pk):
    return row_pk[0] if len(coll["id"]) == 1 else {k: v for k, v in zip(coll["id"], row_pk)}


def _key(v):
    return canon(v)


# ------------------------------------------------------------------ check 1
def check_collections(pg, db, schema, sample_limit=None):
    results = []
    for c in schema["collections"]:
        t, pk = c["source"], c["id"]
        sel = [f"s.{q(k)}" for k in pk] + [f"s.{q(f['from'])}" for f in c.get("fields", [])]
        names = [f["name"] for f in c.get("fields", [])]
        for a in c.get("embedArrays", []):
            sel.append(f"COALESCE((SELECT array_agg(x.{q(a['valueColumn'])} ORDER BY x.{q(a['valueColumn'])}) FROM {q(a['fromTable'])} x "
                       f"WHERE x.{q(a['parentKey'])} = s.{q(pk[0])}), '{{}}')")
            names.append(a["name"])
        for k in c.get("counters", []):
            sel.append(f"(SELECT count(*) FROM {q(k['fromTable'])} x WHERE x.{q(k['parentKey'])} = s.{q(pk[0])})")
            names.append(k["name"])
        order = ", ".join(f"s.{q(k)}" for k in pk)
        sql = f"SELECT {', '.join(sel)} FROM {q(t)} s ORDER BY {order}" + (f" LIMIT {sample_limit}" if sample_limit else "")
        proj = {n: 1 for n in names}
        mismatches, n_rows, examples = 0, 0, []
        mongo_docs = {_key(d["_id"]): d for d in db[c["name"]].find({}, proj)}  # the MQL side: find + projection
        for row in pg.execute(sql):
            n_rows += 1
            _id = _id_of(c, row[: len(pk)])
            doc = mongo_docs.pop(_key(_id), None)
            exp = dict(zip(names, row[len(pk):]))
            if doc is None:
                mismatches += 1
                examples.append(f"{c['name']} _id={_id}: missing document")
                continue
            for n in names:
                a_, b_ = exp[n], doc.get(n)
                if isinstance(a_, list) and isinstance(b_, list):
                    a_, b_ = sorted(a_), sorted(b_)
                if canon(a_) != canon(b_):
                    mismatches += 1
                    if len(examples) < 5:
                        examples.append(f"{c['name']} _id={_id}.{n}: sql={canon(a_)[:60]} mql={canon(b_)[:60]}")
                    break
        extra = 0 if sample_limit else len(mongo_docs)
        results.append({"collection": c["name"], "source": t, "rows": n_rows, "mismatched": mismatches,
                        "extra_documents": extra, "examples": examples, "ok": mismatches == 0 and extra == 0})
    return results


# ------------------------------------------------------------------ check 2
def references(inv, schema):
    """Every place a target document points at another one, derived from Postgres FKs + the schema mapping."""
    by_source = {c["source"]: c for c in schema["collections"]}
    refs = []
    for c in schema["collections"]:
        fks = {fk["column"]: fk["references"].split(".")[0] for fk in inv["postgres"][c["source"]]["foreignKeys"]}
        for col, target in fks.items():
            tgt = by_source.get(target)
            if not tgt:
                continue
            if col in c["id"] and len(c["id"]) > 1:
                refs.append({"from": c["name"], "path": f"_id.{col}", "to": tgt["name"], "array": False})
            for f in c.get("fields", []):
                if f["from"] == col:
                    refs.append({"from": c["name"], "path": f["name"], "to": tgt["name"], "array": False})
        for a in c.get("embedArrays", []):
            fks2 = {fk["column"]: fk["references"].split(".")[0] for fk in inv["postgres"][a["fromTable"]]["foreignKeys"]}
            tgt = by_source.get(fks2.get(a["valueColumn"]))
            if tgt:
                refs.append({"from": c["name"], "path": a["name"], "to": tgt["name"], "array": True})
    return refs


def check_references(db, inv, schema):
    out = []
    for r in references(inv, schema):
        pipeline = [{"$project": {"v": f"${r['path']}"}}]
        if r["array"]:
            pipeline.append({"$unwind": "$v"})
        pipeline += [{"$match": {"v": {"$ne": None}}}, {"$group": {"_id": "$v"}}]
        base = list(pipeline)
        pipeline += [{"$lookup": {"from": r["to"], "localField": "_id", "foreignField": "_id", "as": "t"}},
                     {"$match": {"t": {"$size": 0}}}, {"$count": "dangling"}]
        dangling = next(db[r["from"]].aggregate(pipeline), {}).get("dangling", 0)
        n_distinct = next(db[r["from"]].aggregate(base + [{"$count": "n"}]), {}).get("n", 0)
        out.append({**r, "distinct_referenced": n_distinct, "dangling": dangling, "ok": dangling == 0})
    return out


# ------------------------------------------------------------------ check 3
def check_left_behind(pg, db, inv, schema):
    """Per source table: count + per-column checksum, Postgres vs MongoDB."""
    out = []
    homes = {}  # table -> how to read it back from Mongo
    for c in schema["collections"]:
        homes[c["source"]] = ("collection", c)
        for a in c.get("embedArrays", []):
            homes[a["fromTable"]] = ("embedded", c, a)
    for table, meta in inv["postgres"].items():
        pk = meta["primaryKey"]
        cols = list(meta["columns"])
        home = homes.get(table)
        pg_sums = {col: Checksum() for col in cols}
        pg_count = 0
        for row in pg.execute(f"SELECT {', '.join(q(x) for x in cols)} FROM {q(table)}"):
            pg_count += 1
            r = dict(zip(cols, row))
            key = [r[k] for k in pk]
            for col in cols:
                pg_sums[col].add(*key, col, r[col])
        mg_sums = {col: Checksum() for col in cols}
        mg_count = 0
        if home is None:
            pass
        elif home[0] == "collection":
            c = home[1]
            colmap = {f["from"]: f["name"] for f in c.get("fields", [])}
            for d in db[c["name"]].find({}):
                mg_count += 1
                vals = {}
                if len(pk) == 1:
                    vals[pk[0]] = d["_id"]
                else:
                    vals.update(d["_id"])
                for col in cols:
                    if col not in vals:
                        vals[col] = d.get(colmap.get(col, col))
                key = [vals[k] for k in pk]
                for col in cols:
                    mg_sums[col].add(*key, col, vals[col])
        else:
            c, a = home[1], home[2]
            for d in db[c["name"]].find({}, {a["name"]: 1}):
                for v in d.get(a["name"], []):
                    mg_count += 1
                    vals = {a["parentKey"]: d["_id"], a["valueColumn"]: v}
                    key = [vals[k] for k in pk]
                    for col in cols:
                        mg_sums[col].add(*key, col, vals[col])
        bad = [col for col in cols if pg_sums[col].hex() != mg_sums[col].hex()]
        out.append({"table": table, "stored_as": home[0] + ":" + home[1]["name"] if home else "NOT MIGRATED",
                    "pg_rows": pg_count, "mongo_rows": mg_count, "columns_checked": len(cols),
                    "columns_mismatched": bad, "checksums": {col: pg_sums[col].hex() for col in cols},
                    "ok": pg_count == mg_count and not bad and home is not None})
    return out


def run_all(db_name=None, schema=None, inv=None):
    schema = schema or json.loads((settings.root / "design/schema.json").read_text())
    inv = inv or json.loads((settings.root / "analysis/inventory.json").read_text())
    db = MongoClient(settings.mongo_uri)[db_name or settings.mongo_db]
    with psycopg.connect(settings.pg_dsn) as pg:
        r = {"check1_collections": check_collections(pg, db, schema),
             "check2_references": check_references(db, inv, schema),
             "check3_left_behind": check_left_behind(pg, db, inv, schema)}
    r["ok"] = all(x["ok"] for k in ("check1_collections", "check2_references", "check3_left_behind") for x in r[k])
    return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    res = run_all(a.db)
    for k in ("check1_collections", "check2_references", "check3_left_behind"):
        print(k)
        for x in res[k]:
            print("  ", "OK  " if x["ok"] else "FAIL", {kk: v for kk, v in x.items() if kk not in ("checksums", "ok")})
    print("ALL OK" if res["ok"] else "CHECKS FAILED")
