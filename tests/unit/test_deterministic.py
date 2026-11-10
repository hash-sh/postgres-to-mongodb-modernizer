"""Unit tests for the deterministic parts: normalizer/diff, guard, schema lint, migrator, the three checks.

The migrator/check tests need the docker-compose databases running. They use their own scratch
databases and a tiny seeded Postgres dataset, then deliberately corrupt MongoDB to prove each
check actually FAILS when data is wrong (a check that cannot fail proves nothing).

Run: .venv/bin/python -m pytest tests/unit -q
"""
import copy
import json

import pytest

from pipeline import guard, httpcheck, schema_dsl
from pipeline.config import settings

INV = json.loads((settings.root / "analysis/inventory.json").read_text())
SCHEMA = json.loads((settings.root / "design/schema.json").read_text())


# ------------------------------------------------------------------ normalizer + diff (differential test core)
def test_normalize_tokens_timestamps_unordered_and_500():
    body = {"user": {"token": "eyJabc", "createdAt": "2026-11-10T10:00:00.123Z"}, "tags": ["b", "a"]}
    n = httpcheck.normalize(body, 200)
    assert n == {"user": {"token": "<token>", "createdAt": "<ts>"}, "tags": ["a", "b"]}
    assert body["tags"] == ["b", "a"], "normalize must not mutate its input"
    err = httpcheck.normalize({"errors": {"body": ["Cannot read properties of null"]}}, 500)
    assert err == {"errors": {"body": ["<internal error>"]}}
    # 4xx messages are part of the contract and are NOT normalized
    assert httpcheck.normalize({"errors": {"body": ["Article not found "]}}, 404)["errors"]["body"] == ["Article not found "]


def test_normalize_ts_cutoff_keeps_migrated_timestamps_strict():
    b = {"a": "2025-01-01T00:00:00.000Z", "b": "2026-11-10T12:00:00.000Z"}
    n = httpcheck.normalize(b, 200, ts_cutoff="2026-11-10T00:00:00")
    assert n == {"a": "2025-01-01T00:00:00.000Z", "b": "<ts>"}


def test_diff_reports_missing_extra_and_changed():
    d = httpcheck.diff({"a": 1, "b": [1, 2], "c": {"x": 1}}, {"a": 2, "b": [1], "d": 0, "c": {"x": 1}})
    assert "$.a: expected 1 got 2" in d
    assert any("expected list of 2 got 1" in x for x in d)
    assert any("$.d: unexpected key" in x for x in d)
    assert httpcheck.diff({"x": [1, {"y": None}]}, {"x": [1, {"y": None}]}) == []


def test_compare_counts_steps():
    g = {"s": [{"step": "a", "request": "GET /x", "status": 200, "body": {"k": 1}},
               {"step": "b", "request": "GET /y", "status": 404, "body": {}}]}
    r = {"s": [{"status": 200, "body": {"k": 1}}, {"status": 500, "body": {}}]}
    p, t, f = httpcheck.compare(g, r)
    assert (p, t) == (1, 2) and f[0]["step"] == "b" and "status: expected 404 got 500" in f[0]["diffs"]


# ------------------------------------------------------------------ guard
def test_safe_writer_allowlist(tmp_path):
    w = guard.SafeWriter(tmp_path, ["controllers/*.js", "index.js"])
    w.write("controllers/a.js", "x")
    assert (tmp_path / "controllers/a.js").read_text() == "x"
    for bad in ["../evil.js", "/etc/passwd", "tests/characterization/scenarios.py", "routes/users.js",
                "controllers/../../x.js", "helper/customErrors.js", "package.json"]:
        with pytest.raises(guard.GuardError):
            w.write(bad, "x")


def test_frozen_suite_detects_tampering(tmp_path, monkeypatch):
    d = tmp_path / "tests/characterization"
    d.mkdir(parents=True)
    (d / "t.py").write_text("assert True")
    monkeypatch.setattr(guard, "FROZEN_DIRS", ["tests/characterization"])
    monkeypatch.setattr(guard, "MANIFEST", tmp_path / "tests/MANIFEST.sha256")
    import pipeline.guard as g
    orig_root = g.settings.root
    object.__setattr__(g.settings, "root", tmp_path)
    try:
        h = g.freeze()
        g.verify_tests(h)  # untouched: passes
        (d / "t.py").write_text("assert True  # weakened")
        with pytest.raises(g.GuardError, match="CHANGED"):
            g.verify_tests(h)
        (d / "t.py").write_text("assert True")
        g.MANIFEST.write_text(g.MANIFEST.read_text() + "x")
        with pytest.raises(g.GuardError, match="MANIFEST WAS MODIFIED"):
            g.verify_tests(h)
    finally:
        object.__setattr__(g.settings, "root", orig_root)


# ------------------------------------------------------------------ schema lint (failure modes from the paper)
PROFILE = {"maxRowBytes": {"Users": 240, "Articles": 972, "Comments": 264, "Tags": 35, "TagList": 39, "Favorites": 32, "Followers": 32},
           "fanout": {"TagList.articleId": {"max": 4}, "Favorites.articleId": {"max": 20}, "Followers.userId": {"max": 43},
                      "Comments.articleId": {"max": 6}}}


def _strip(s):
    s = copy.deepcopy(s)
    s.pop("generatedBy", None)
    for c in s["collections"]:
        c.pop("estimatedMaxDocBytes", None)
    return s


def test_final_schema_passes_lint():
    E, _ = schema_dsl.lint(_strip(SCHEMA), INV, PROFILE)
    assert E == []


def test_lint_rejects_self_referential_and_unbounded_embedding():
    s = _strip(SCHEMA)
    users = next(c for c in s["collections"] if c["name"] == "users")
    users["embedArrays"] = [{"name": "followers", "fromTable": "Followers", "parentKey": "userId",
                             "valueColumn": "followerId", "maxItems": 100}]
    E, _ = schema_dsl.lint(s, INV, PROFILE)
    assert any("self-referential" in e for e in E)
    assert any("UNBOUNDED" in e for e in E)


def test_lint_rejects_16mb_documents_and_lost_columns_and_missing_index():
    s = _strip(SCHEMA)
    arts = next(c for c in s["collections"] if c["name"] == "articles")
    arts["embedArrays"][0]["maxItems"] = 300000
    arts["fields"] = [f for f in arts["fields"] if f["from"] != "body"]
    arts["indexes"] = [i for i in arts["indexes"] if "slug" not in i["keys"]]
    E, _ = schema_dsl.lint(s, INV, PROFILE)
    assert any("16 MB" in e for e in E)
    assert any("['body']" in e and "not recoverable" in e for e in E)
    assert any("no index on articles for query ['slug']" in e for e in E)


def test_naive_schema_is_recoverable_but_unindexed():
    E, _ = schema_dsl.lint(schema_dsl.naive_schema(INV), INV, PROFILE)
    assert E and all("(R10)" in e for e in E)


# ------------------------------------------------------------------ migrator + the three checks (integration)
@pytest.fixture(scope="module")
def migrated():
    try:
        import psycopg
        from pymongo import MongoClient
        MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=2000).admin.command("ping")
        psycopg.connect(settings.pg_dsn, connect_timeout=2).close()
    except Exception:
        pytest.skip("databases not running (docker compose up -d)")
    from pipeline import migrator, seed
    seed.seed(60)
    db_name = "unit_test_migration"
    from pymongo import MongoClient
    c = MongoClient(settings.mongo_uri)
    c.drop_database(db_name)
    c.drop_database(db_name + "__migration")
    s1 = migrator.migrate(SCHEMA, db_name, fresh=True, workers=4, log=lambda *_: None)
    yield c[db_name], s1
    c.drop_database(db_name)
    c.drop_database(db_name + "__migration")


def test_migrator_idempotent_and_resumable(migrated):
    from pipeline import migrator
    db, s1 = migrated
    before = {n: list(db[n].find().sort("_id", 1)) for n in db.list_collection_names()}
    s2 = migrator.migrate(SCHEMA, db.name, fresh=False, workers=4, log=lambda *_: None)
    assert s2["batches_skipped"] == s2["batches"] and s2["rows_written"] == 0  # resume: nothing redone
    s3 = migrator.migrate(SCHEMA, db.name, fresh=True, workers=4, log=lambda *_: None)
    assert s3["rows_written"] == s1["rows_written"]
    after = {n: list(db[n].find().sort("_id", 1)) for n in db.list_collection_names()}
    assert before == after  # re-running from scratch produces byte-identical documents, no duplicates


def _checks(db):
    import psycopg
    from pipeline import checks
    with psycopg.connect(settings.pg_dsn) as pg:
        return (checks.check_collections(pg, db, SCHEMA), checks.check_references(db, INV, SCHEMA),
                checks.check_left_behind(pg, db, INV, SCHEMA))


def test_three_checks_pass_on_clean_migration(migrated):
    db, _ = migrated
    c1, c2, c3 = _checks(db)
    assert all(x["ok"] for x in c1 + c2 + c3)


def test_checks_detect_corruption(migrated):
    from pipeline import migrator
    db, _ = migrated
    a = db.articles.find_one()
    db.articles.update_one({"_id": a["_id"]}, {"$set": {"title": "tampered"}})             # changed value
    db.comments.delete_one({})                                                             # lost row
    db.favorites.insert_one({"_id": {"articleId": 999999, "userId": 1}})                    # dangling ref
    db.articles.update_one({"_id": a["_id"]}, {"$push": {"tagList": "ghost-tag"}})           # extra embedded value
    c1, c2, c3 = _checks(db)
    by = {x["collection"]: x for x in c1}
    assert not by["articles"]["ok"] and not by["comments"]["ok"] and not by["favorites"]["ok"]
    assert any(not x["ok"] and x["path"] == "_id.articleId" for x in c2)
    assert any(not x["ok"] and x["path"] == "tagList" for x in c2)
    t3 = {x["table"]: x for x in c3}
    assert t3["Comments"]["pg_rows"] == t3["Comments"]["mongo_rows"] + 1 and not t3["Comments"]["ok"]
    assert "title" in t3["Articles"]["columns_mismatched"]
    assert not t3["TagList"]["ok"] and not t3["Favorites"]["ok"]
    migrator.migrate(SCHEMA, db.name, fresh=True, workers=4, log=lambda *_: None)  # repair by re-running (idempotent)
    db.favorites.delete_one({"_id": {"articleId": 999999, "userId": 1}})
    c1, c2, c3 = _checks(db)
    assert all(x["ok"] for x in c1 + c2 + c3)
