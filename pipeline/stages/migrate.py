"""Stage 5 - Data migration + validation (100% deterministic).

1. Seed Postgres (validation dataset) deterministically.
2. Migrate into MongoDB (single-node replica set), then run the migrator AGAIN to prove idempotency
   (identical documents, nothing rewritten on resume).
3. The paper's three checks (pipeline/checks.py).
4. Behavioural validation on the final MongoDB backend: frozen characterization tests + official suite.
5. Differential test: same request sequence on both backends over the identical migrated data.
"""
import hashlib
import json

from pymongo import MongoClient

from pipeline import apps, checks, differential, migrator, seed, testrun
from pipeline.config import settings
from pipeline.httpcheck import now_iso

OUT = settings.root / "reports/raw"


def db_fingerprint(db):
    h = hashlib.sha256()
    for name in sorted(n for n in db.list_collection_names()):
        for d in db[name].find().sort("_id", 1):
            h.update(repr(sorted(d.items())).encode())
    return h.hexdigest()[:24]


def run(state):
    OUT.mkdir(parents=True, exist_ok=True)
    schema = json.loads((settings.root / "design/schema.json").read_text())
    rep = {}
    # 1+2: seed, migrate, migrate again
    with apps.original():  # make sure the original's tables exist (sequelize.sync)
        pass
    rep["seed"] = seed.seed(settings.validation_articles)
    print(f"  seeded Postgres: {rep['seed']}")
    client = MongoClient(settings.mongo_uri)
    client.drop_database(settings.mongo_db)
    rep["migration"] = migrator.migrate(schema, settings.mongo_db, fresh=True)
    fp1 = db_fingerprint(client[settings.mongo_db])
    rep["migration_resume"] = migrator.migrate(schema, settings.mongo_db, fresh=False)
    rep["migration_rerun"] = migrator.migrate(schema, settings.mongo_db, fresh=True)
    fp2 = db_fingerprint(client[settings.mongo_db])
    rep["idempotent"] = fp1 == fp2
    print(f"  migrated {rep['migration']['rows_written']} docs ({rep['migration']['rows_per_second']} rows/s); "
          f"resume skipped {rep['migration_resume']['batches_skipped']}/{rep['migration_resume']['batches']} batches; "
          f"re-run identical: {rep['idempotent']}")
    # 3: three checks
    ck = checks.run_all(settings.mongo_db, schema)
    (OUT / "checks.json").write_text(json.dumps(ck, indent=1, default=str))
    rep["checks_ok"] = ck["ok"]
    for k in ("check1_collections", "check2_references", "check3_left_behind"):
        print(f"  {k}: {sum(x['ok'] for x in ck[k])}/{len(ck[k])} OK")
    # 4: frozen tests + official suite on the final backend (separate CI database)
    final = testrun.run_slice(state, 3)
    rep["characterization"] = f"{final['char_passed']}/{final['char_total']}"
    rep["official_mongo"] = f"{final['official_passed']}/{final['official_total']}"
    rep["official_mismatch_vs_original"] = final["official_mismatch"]
    rep["characterization_failures"] = final["char_failures"]
    print(f"  characterization {rep['characterization']}, official {rep['official_mongo']} "
          f"(entries behaving differently from original: {len(final['official_mismatch'])})")
    # 5: differential on the migrated data (both DBs freshly identical)
    rep["seed2"] = seed.seed(settings.validation_articles)
    client.drop_database(settings.mongo_db)
    migrator.migrate(schema, settings.mongo_db, fresh=True, log=lambda *_: None)
    seq = differential.build_sequence()
    cutoff = now_iso()
    with apps.original() as o, apps.mongo(db=settings.mongo_db) as m:
        diff = differential.run(o.url, m.url, seq, cutoff)
    (OUT / "differential.json").write_text(json.dumps({"sequence": seq, **diff}, indent=1, default=str))
    rep["differential"] = {k: diff[k] for k in ("requests", "matched", "match_rate")}
    print(f"  differential: {diff['matched']}/{diff['requests']} responses identical")
    for mm in diff["mismatches"][:5]:
        print(f"    MISMATCH {mm['request']}: {mm['diffs'][:2]}")
    (OUT / "migrate_stage.json").write_text(json.dumps(rep, indent=1, default=str))
    ok = rep["checks_ok"] and rep["idempotent"] and final["ok"]
    state.set_stage("migrate", "done" if ok else "failed", checks_ok=rep["checks_ok"], idempotent=rep["idempotent"],
                    characterization=rep["characterization"], differential=f"{diff['matched']}/{diff['requests']}")
