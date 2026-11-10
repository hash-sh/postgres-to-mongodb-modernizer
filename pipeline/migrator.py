"""Postgres -> MongoDB data migrator. 100% deterministic, interprets design/schema.json.

* Batched:     each collection's source table is cut into PK ranges of BATCH rows (keyset ranges, no OFFSET).
* Parallel:    batches run on a thread pool, each worker with its own Postgres connection.
* Idempotent:  _id is derived from the source primary key and every write is ReplaceOne(upsert=True),
               so re-running produces exactly the same documents - never duplicates.
* Resumable:   after a batch is written, a checkpoint {collection, range, rows} is stored in the
               `<db>__migration` database. A re-run skips batches that already have a checkpoint
               (use --fresh to ignore them).

Usage: python -m pipeline.migrator [--schema design/schema.json] [--db conduit] [--fresh] [--workers 8]
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone

import psycopg
from pymongo import IndexModel, MongoClient, ReplaceOne

from pipeline.config import settings

BATCH = 2000
SEQUENCES = {"users": "Users", "articles": "Articles", "comments": "Comments"}


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def _boundaries(conn, table, pk):
    """PK values that start each batch (single-column PK) or row numbers (compound PK)."""
    if len(pk) == 1:
        rows = conn.execute(f"SELECT {q(pk[0])} FROM (SELECT {q(pk[0])}, row_number() OVER (ORDER BY {q(pk[0])}) rn "
                            f"FROM {q(table)}) s WHERE (rn - 1) % {BATCH} = 0 ORDER BY 1").fetchall()
        starts = [r[0] for r in rows]
        return [(starts[i], starts[i + 1] if i + 1 < len(starts) else None) for i in range(len(starts))]
    n = conn.execute(f"SELECT count(*) FROM {q(table)}").fetchone()[0]
    return [(i, i + BATCH) for i in range(0, n, BATCH)]


def _fetch(conn, coll, rng):
    t, pk = coll["source"], coll["id"]
    cols = list(dict.fromkeys(pk + [f["from"] for f in coll.get("fields", [])] +
                              [r["localKey"] for r in coll.get("extendedRefs", [])]))
    sel = ", ".join(q(c) for c in cols)
    if len(pk) == 1:
        lo, hi = rng
        where = f"WHERE {q(pk[0])} >= %s" + (f" AND {q(pk[0])} < %s" if hi is not None else "")
        args = [lo] + ([hi] if hi is not None else [])
        rows = conn.execute(f"SELECT {sel} FROM {q(t)} {where} ORDER BY {q(pk[0])}", args).fetchall()
    else:
        order = ", ".join(q(c) for c in pk)
        rows = conn.execute(f"SELECT {sel} FROM {q(t)} ORDER BY {order} OFFSET %s LIMIT %s", [rng[0], BATCH]).fetchall()
    return cols, rows


def _norm(v):
    if hasattr(v, "tzinfo") and v is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v  # naive UTC for BSON
    return v


def build_docs(conn, coll, cols, rows):
    """Turn source rows into documents (pure function of the source data + schema)."""
    pk = coll["id"]
    ix = {c: i for i, c in enumerate(cols)}
    ids = [r[ix[pk[0]]] for r in rows] if len(pk) == 1 else []
    embeds, counts, refs = {}, {}, {}
    for a in coll.get("embedArrays", []):
        res = conn.execute(f"SELECT {q(a['parentKey'])}, {q(a['valueColumn'])} FROM {q(a['fromTable'])} "
                           f"WHERE {q(a['parentKey'])} = ANY(%s) ORDER BY 1, 2", [ids]).fetchall()
        m = {}
        for p, v in res:
            m.setdefault(p, []).append(v)
        embeds[a["name"]] = m
    for k in coll.get("counters", []):
        res = conn.execute(f"SELECT {q(k['parentKey'])}, count(*) FROM {q(k['fromTable'])} "
                           f"WHERE {q(k['parentKey'])} = ANY(%s) GROUP BY 1", [ids]).fetchall()
        counts[k["name"]] = dict(res)
    for r in coll.get("extendedRefs", []):
        fks = list({row[ix[r["localKey"]]] for row in rows if row[ix[r["localKey"]]] is not None})
        sel = ", ".join(q(x) for x in ["id"] + r["copy"])
        res = conn.execute(f"SELECT {sel} FROM {q(r['fromTable'])} WHERE id = ANY(%s)", [fks]).fetchall()
        refs[r["name"]] = {x[0]: dict(zip(["id"] + r["copy"], x)) for x in res}
    docs = []
    for row in rows:
        _id = row[ix[pk[0]]] if len(pk) == 1 else {c: row[ix[c]] for c in pk}
        d = {"_id": _id}
        for f in coll.get("fields", []):
            d[f["name"]] = _norm(row[ix[f["from"]]])
        for a in coll.get("embedArrays", []):
            d[a["name"]] = embeds[a["name"]].get(_id, [])
        for k in coll.get("counters", []):
            d[k["name"]] = counts[k["name"]].get(_id, 0)
        for r in coll.get("extendedRefs", []):
            fk = row[ix[r["localKey"]]]
            d[r["name"]] = refs[r["name"]].get(fk) if fk is not None else None
        docs.append(d)
    return docs


def create_indexes(db, coll):
    models = [IndexModel(list(i["keys"].items()), unique=bool(i.get("unique"))) for i in coll.get("indexes", [])]
    if models:
        db[coll["name"]].create_indexes(models)


def migrate(schema, db_name=None, fresh=False, workers=8, log=print):
    db_name = db_name or settings.mongo_db
    client = MongoClient(settings.mongo_uri)
    db = client[db_name]
    meta = client[f"{db_name}__migration"]["checkpoints"]
    if fresh:
        meta.delete_many({})
    stats = {"db": db_name, "collections": {}, "started": time.time()}
    total_rows = 0
    t_all = time.time()
    with psycopg.connect(settings.pg_dsn) as conn:
        plan = [(c, rng) for c in schema["collections"] for rng in _boundaries(conn, c["source"], c["id"])]
    done = {d["_id"] for d in meta.find({}, {"_id": 1})}
    todo = [(c, rng) for c, rng in plan if f"{c['name']}:{rng[0]}" not in done]
    log(f"  migrating into '{db_name}': {len(plan)} batches ({len(plan) - len(todo)} already checkpointed), {workers} workers")

    def work(c, rng):
        with psycopg.connect(settings.pg_dsn) as conn:
            cols, rows = _fetch(conn, c, rng)
            docs = build_docs(conn, c, cols, rows)
        if docs:
            db[c["name"]].bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs], ordered=False)
        meta.replace_one({"_id": f"{c['name']}:{rng[0]}"}, {"_id": f"{c['name']}:{rng[0]}", "collection": c["name"],
                                                            "rows": len(docs), "at": time.time()}, upsert=True)
        return c["name"], len(docs)

    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(work, c, rng) for c, rng in todo]
        for f in as_completed(futs):
            name, n = f.result()
            stats["collections"][name] = stats["collections"].get(name, 0) + n
            total_rows += n
    t_load = time.time() - t_all
    for c in schema["collections"]:
        create_indexes(db, c)
    # Auto-increment counters continue exactly where the Postgres sequences are.
    with psycopg.connect(settings.pg_dsn) as conn:
        for coll, table in SEQUENCES.items():
            if any(c["name"] == coll for c in schema["collections"]):
                last, called = conn.execute(f"SELECT last_value, is_called FROM {q(table + '_id_seq')}").fetchone()
                db["counters"].replace_one({"_id": coll}, {"_id": coll, "seq": last if called else last - 1}, upsert=True)
    stats.update(rows_written=total_rows, load_seconds=round(t_load, 2), total_seconds=round(time.time() - t_all, 2),
                 rows_per_second=round(total_rows / t_load, 1) if t_load > 0 and total_rows else None,
                 batches=len(plan), batches_skipped=len(plan) - len(todo))
    stats.pop("started")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default=str(settings.root / "design/schema.json"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    print(json.dumps(migrate(json.load(open(a.schema)), a.db, a.fresh, a.workers), indent=2))
