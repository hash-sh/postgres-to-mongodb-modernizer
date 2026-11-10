"""Stage 6a - Benchmark (deterministic). Every number written here comes from this run.

Dataset: pipeline.seed at BENCH_ARTICLES (default 100k articles, ~20k users) in Postgres, then migrated
twice: into `conduit_bench` (designed schema) and `conduit_naive` (one-to-one, no secondary indexes).

Two levels of measurement, both p50/p95 over sequential requests after warm-up:
  A. HTTP end-to-end: the original app (Sequelize/Postgres) vs the generated app (Mongoose/designed schema).
  B. Data-access level, 3-way (Postgres SQL, naive Mongo, designed Mongo): the SAME batched query strategy
     per endpoint (fetch page -> batch-load authors/tags/counts/viewer state), implemented once per store,
     so differences come from the SCHEMA (embedding, counters, indexes), not from app code style.
"""
import json
import statistics
import time

import psycopg
import requests
from pymongo import MongoClient

from pipeline import apps, migrator, seed
from pipeline.config import settings
from pipeline.seed import SEED_PASSWORD

OUT = settings.root / "reports/raw"
BENCH_DB, NAIVE_DB = "conduit_bench", "conduit_naive"


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 2)


def timeit(fn, n, warmup=5, budget_s=60):
    for _ in range(warmup):
        fn()
    ts, t_end = [], time.time() + budget_s
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
        if time.time() > t_end:
            break
    return {"n": len(ts), "p50_ms": pct(ts, 50), "p95_ms": pct(ts, 95), "mean_ms": round(statistics.mean(ts), 2)}


# ------------------------------------------------------------------ B. data-access implementations
class PG:
    name = "postgres"

    def __init__(self):
        self.c = psycopg.connect(settings.pg_dsn, autocommit=True)

    def _decorate(self, arts, viewer):
        ids = [a[0] for a in arts]
        authors = list({a[1] for a in arts})
        tags = self.c.execute('SELECT "articleId", array_agg("tagName") FROM "TagList" WHERE "articleId" = ANY(%s) GROUP BY 1', [ids]).fetchall()
        favc = self.c.execute('SELECT "articleId", count(*) FROM "Favorites" WHERE "articleId" = ANY(%s) GROUP BY 1', [ids]).fetchall()
        us = self.c.execute('SELECT id, username, bio, image FROM "Users" WHERE id = ANY(%s)', [authors]).fetchall()
        folc = self.c.execute('SELECT "userId", count(*) FROM "Followers" WHERE "userId" = ANY(%s) GROUP BY 1', [authors]).fetchall()
        mine = self.c.execute('SELECT "articleId" FROM "Favorites" WHERE "userId"=%s AND "articleId" = ANY(%s)', [viewer, ids]).fetchall()
        fol = self.c.execute('SELECT "userId" FROM "Followers" WHERE "followerId"=%s AND "userId" = ANY(%s)', [viewer, authors]).fetchall()
        return len(tags) + len(favc) + len(us) + len(folc) + len(mine) + len(fol)

    def list(self, viewer, tag=None, author=None):
        if tag:
            arts = self.c.execute('SELECT a.id, a."userId" FROM "Articles" a JOIN "TagList" t ON t."articleId"=a.id '
                                  'WHERE t."tagName"=%s ORDER BY a."createdAt" DESC LIMIT 20', [tag]).fetchall()
        elif author:
            arts = self.c.execute('SELECT a.id, a."userId" FROM "Articles" a JOIN "Users" u ON u.id=a."userId" '
                                  'WHERE u.username=%s ORDER BY a."createdAt" DESC LIMIT 20', [author]).fetchall()
        else:
            arts = self.c.execute('SELECT id, "userId" FROM "Articles" ORDER BY "createdAt" DESC LIMIT 20').fetchall()
        self.c.execute('SELECT count(*) FROM "Articles"').fetchone()
        return self._decorate(arts, viewer)

    def feed(self, viewer):
        arts = self.c.execute('SELECT a.id, a."userId" FROM "Articles" a WHERE a."userId" IN '
                              '(SELECT "userId" FROM "Followers" WHERE "followerId"=%s) ORDER BY a."createdAt" DESC LIMIT 20', [viewer]).fetchall()
        return self._decorate(arts, viewer)

    def article(self, viewer, slug):
        arts = self.c.execute('SELECT id, "userId" FROM "Articles" WHERE slug=%s', [slug]).fetchall()
        com = self.c.execute('SELECT c.id, c."userId" FROM "Comments" c WHERE c."articleId"=%s ORDER BY c.id', [arts[0][0]]).fetchall()
        cu = list({x[1] for x in com})
        self.c.execute('SELECT id FROM "Users" WHERE id = ANY(%s)', [cu]).fetchall()
        self.c.execute('SELECT "userId", count(*) FROM "Followers" WHERE "userId" = ANY(%s) GROUP BY 1', [cu]).fetchall()
        return self._decorate(arts, viewer) + len(com)

    def profile(self, viewer, username):
        u = self.c.execute('SELECT id FROM "Users" WHERE username=%s', [username]).fetchone()
        self.c.execute('SELECT count(*) FROM "Followers" WHERE "userId"=%s', [u[0]]).fetchone()
        self.c.execute('SELECT 1 FROM "Followers" WHERE "userId"=%s AND "followerId"=%s', [u[0], viewer]).fetchone()
        return 1


class Naive:
    """One-to-one collections named like the tables, same column names, NO secondary indexes."""
    name = "mongo_naive"

    def __init__(self):
        self.db = MongoClient(settings.mongo_uri)[NAIVE_DB]

    def _decorate(self, arts, viewer):
        d = self.db
        ids = [a["_id"] for a in arts]
        authors = list({a["userId"] for a in arts})
        n = len(list(d.TagList.find({"_id.articleId": {"$in": ids}})))
        n += len(list(d.Favorites.aggregate([{"$match": {"_id.articleId": {"$in": ids}}}, {"$group": {"_id": "$_id.articleId", "n": {"$sum": 1}}}])))
        n += len(list(d.Users.find({"_id": {"$in": authors}}, {"username": 1, "bio": 1, "image": 1})))
        n += len(list(d.Followers.aggregate([{"$match": {"_id.userId": {"$in": authors}}}, {"$group": {"_id": "$_id.userId", "n": {"$sum": 1}}}])))
        n += len(list(d.Favorites.find({"_id.userId": viewer, "_id.articleId": {"$in": ids}})))
        n += len(list(d.Followers.find({"_id.followerId": viewer, "_id.userId": {"$in": authors}})))
        return n

    def list(self, viewer, tag=None, author=None):
        d = self.db
        if tag:
            ids = [t["_id"]["articleId"] for t in d.TagList.find({"_id.tagName": tag}, {"_id": 1})]
            arts = list(d.Articles.find({"_id": {"$in": ids}}, {"userId": 1}).sort("createdAt", -1).limit(20))
        elif author:
            u = d.Users.find_one({"username": author})
            arts = list(d.Articles.find({"userId": u["_id"]}, {"userId": 1}).sort("createdAt", -1).limit(20))
        else:
            arts = list(d.Articles.find({}, {"userId": 1}).sort("createdAt", -1).limit(20))
        d.Articles.estimated_document_count()
        return self._decorate(arts, viewer)

    def feed(self, viewer):
        d = self.db
        authors = [f["_id"]["userId"] for f in d.Followers.find({"_id.followerId": viewer})]
        arts = list(d.Articles.find({"userId": {"$in": authors}}, {"userId": 1}).sort("createdAt", -1).limit(20))
        return self._decorate(arts, viewer)

    def article(self, viewer, slug):
        d = self.db
        arts = list(d.Articles.find({"slug": slug}, {"userId": 1}))
        com = list(d.Comments.find({"articleId": arts[0]["_id"]}, {"userId": 1}).sort("_id", 1))
        cu = list({c["userId"] for c in com})
        list(d.Users.find({"_id": {"$in": cu}}))
        list(d.Followers.aggregate([{"$match": {"_id.userId": {"$in": cu}}}, {"$group": {"_id": "$_id.userId", "n": {"$sum": 1}}}]))
        return self._decorate(arts, viewer) + len(com)

    def profile(self, viewer, username):
        d = self.db
        u = d.Users.find_one({"username": username})
        d.Followers.count_documents({"_id.userId": u["_id"]})
        d.Followers.find_one({"_id.userId": u["_id"], "_id.followerId": viewer})
        return 1


class Designed:
    """The reviewed schema: embedded tagList, favoritesCount/followersCount counters, access-pattern indexes."""
    name = "mongo_designed"

    def __init__(self):
        self.db = MongoClient(settings.mongo_uri)[BENCH_DB]

    def _decorate(self, arts, viewer):
        d = self.db
        ids = [a["_id"] for a in arts]
        authors = list({a["authorId"] for a in arts})
        n = len(list(d.users.find({"_id": {"$in": authors}}, {"username": 1, "bio": 1, "image": 1, "followersCount": 1})))
        n += len(list(d.favorites.find({"_id.userId": viewer, "_id.articleId": {"$in": ids}})))
        n += len(list(d.followers.find({"_id.followerId": viewer, "_id.userId": {"$in": authors}})))
        return n

    def list(self, viewer, tag=None, author=None):
        d = self.db
        q = {}
        if tag:
            q = {"tagList": tag}
        elif author:
            u = d.users.find_one({"username": author}, {"_id": 1})
            q = {"authorId": u["_id"]}
        arts = list(d.articles.find(q, {"authorId": 1, "tagList": 1, "favoritesCount": 1}).sort("createdAt", -1).limit(20))
        d.articles.estimated_document_count()
        return self._decorate(arts, viewer)

    def feed(self, viewer):
        d = self.db
        authors = [f["_id"]["userId"] for f in d.followers.find({"_id.followerId": viewer}, {"_id": 1})]
        arts = list(d.articles.find({"authorId": {"$in": authors}}, {"authorId": 1, "tagList": 1, "favoritesCount": 1}).sort("createdAt", -1).limit(20))
        return self._decorate(arts, viewer)

    def article(self, viewer, slug):
        d = self.db
        arts = list(d.articles.find({"slug": slug}, {"authorId": 1, "tagList": 1, "favoritesCount": 1}))
        com = list(d.comments.find({"articleId": arts[0]["_id"]}, {"authorId": 1}).sort("_id", 1))
        list(d.users.find({"_id": {"$in": list({c["authorId"] for c in com})}}, {"username": 1, "followersCount": 1}))
        return self._decorate(arts, viewer) + len(com)

    def profile(self, viewer, username):
        d = self.db
        u = d.users.find_one({"username": username}, {"followersCount": 1})
        d.followers.find_one({"_id.userId": u["_id"], "_id.followerId": viewer})
        return 1


def run(state):
    OUT.mkdir(parents=True, exist_ok=True)
    n_art = settings.bench_articles
    res = {"dataset": None}
    with apps.original():
        pass  # ensure tables exist
    res["dataset"] = seed.seed(n_art)
    print(f"  benchmark dataset in Postgres: {res['dataset']}")
    schema = json.loads((settings.root / "design/schema.json").read_text())
    naive = json.loads((settings.root / "design/schema_naive.json").read_text())
    cl = MongoClient(settings.mongo_uri)
    for db in (BENCH_DB, NAIVE_DB):
        cl.drop_database(db)
        cl.drop_database(db + "__migration")
    res["migration_designed"] = migrator.migrate(schema, BENCH_DB, fresh=True, workers=8)
    res["migration_naive"] = migrator.migrate(naive, NAIVE_DB, fresh=True, workers=8)
    print(f"  migration designed: {res['migration_designed']['rows_written']} rows in {res['migration_designed']['load_seconds']}s "
          f"({res['migration_designed']['rows_per_second']} rows/s)")

    with psycopg.connect(settings.pg_dsn) as c:
        viewer, vname = c.execute('SELECT u.id, u.username FROM "Users" u JOIN "Followers" f ON f."followerId"=u.id '
                                  'GROUP BY u.id ORDER BY count(*) DESC, u.id LIMIT 1').fetchone()
        author = c.execute('SELECT u.username FROM "Users" u JOIN "Articles" a ON a."userId"=u.id GROUP BY u.id '
                           'ORDER BY count(*) DESC LIMIT 1').fetchone()[0]
        tag = c.execute('SELECT "tagName" FROM "TagList" GROUP BY 1 ORDER BY count(*) DESC LIMIT 1').fetchone()[0]
        slug = c.execute('SELECT a.slug FROM "Articles" a JOIN "Comments" m ON m."articleId"=a.id GROUP BY a.id '
                         'ORDER BY count(*) DESC, a.id LIMIT 1').fetchone()[0]
    res["params"] = {"viewer": vname, "author": author, "tag": tag, "slug": slug}

    # B: data-access level, 3-way
    impls = [PG(), Naive(), Designed()]
    cases = [("article list (default)", lambda i: i.list(viewer)), ("article list ?tag=", lambda i: i.list(viewer, tag=tag)),
             ("article list ?author=", lambda i: i.list(viewer, author=author)), ("feed", lambda i: i.feed(viewer)),
             ("single article + comments", lambda i: i.article(viewer, slug)), ("profile", lambda i: i.profile(viewer, author))]
    res["data_access"] = {}
    for label, fn in cases:
        row = {}
        for impl in impls:
            row[impl.name] = timeit(lambda: fn(impl), n=200, warmup=3, budget_s=45)
        res["data_access"][label] = row
        print(f"  [data access] {label}: " + ", ".join(f"{k} p50={v['p50_ms']}ms p95={v['p95_ms']}ms (n={v['n']})" for k, v in row.items()))

    # A: HTTP end-to-end
    eps = [("GET /api/articles?limit=20", "/api/articles?limit=20"), (f"GET /api/articles?tag={tag}", f"/api/articles?tag={tag}&limit=20"),
           (f"GET /api/articles?author={author}", f"/api/articles?author={author}&limit=20"),
           ("GET /api/articles/feed", "/api/articles/feed?limit=20"), ("GET /api/articles/:slug", f"/api/articles/{slug}"),
           ("GET /api/articles/:slug/comments", f"/api/articles/{slug}/comments"), ("GET /api/profiles/:username", f"/api/profiles/{author}")]
    res["http"] = {}
    for be in (apps.original(), apps.mongo(db=BENCH_DB)):
        with be:
            s = requests.Session()
            tok = s.post(be.url + "/api/users/login", json={"user": {"email": f"{vname}@example.com", "password": SEED_PASSWORD}}).json()["user"]["token"]
            h = {"Authorization": f"Token {tok}"}
            for label, path in eps:
                def call():
                    r = s.get(be.url + path, headers=h, timeout=60)
                    assert r.status_code == 200, (path, r.status_code, r.text[:200])
                res["http"].setdefault(label, {})[be.name] = timeit(call, n=100, warmup=5, budget_s=60)
    # Fairness: the original Sequelize schema has ONLY primary-key indexes in Postgres. Re-measure the data-access
    # level with the same secondary indexes the MongoDB design got, so MongoDB is not credited for indexing alone.
    with psycopg.connect(settings.pg_dsn, autocommit=True) as c:
        for ddl in ['CREATE INDEX IF NOT EXISTS b1 ON "Articles"(slug)', 'CREATE INDEX IF NOT EXISTS b2 ON "Articles"("createdAt" DESC)',
                    'CREATE INDEX IF NOT EXISTS b3 ON "Articles"("userId","createdAt" DESC)', 'CREATE INDEX IF NOT EXISTS b4 ON "TagList"("tagName")',
                    'CREATE INDEX IF NOT EXISTS b5 ON "Comments"("articleId")', 'CREATE INDEX IF NOT EXISTS b6 ON "Followers"("followerId")',
                    'CREATE INDEX IF NOT EXISTS b7 ON "Favorites"("userId")', 'CREATE INDEX IF NOT EXISTS b8 ON "Users"(username)', 'ANALYZE']:
            c.execute(ddl)
    pgi = PG()
    pgi.name = "postgres_indexed"
    for label, fn in cases:
        res["data_access"][label][pgi.name] = timeit(lambda: fn(pgi), n=200, warmup=3, budget_s=45)
        print(f"  [data access] {label}: postgres_indexed p50={res['data_access'][label][pgi.name]['p50_ms']}ms")
    # Remove the fairness indexes again: TRUNCATE keeps indexes, and leaving them would silently change the ORIGINAL
    # app's query plans (and e.g. its unspecified comment order) for every later validation run.
    with psycopg.connect(settings.pg_dsn, autocommit=True) as c:
        c.execute("DROP INDEX IF EXISTS b1, b2, b3, b4, b5, b6, b7, b8")
    for label, row in res["http"].items():
        print(f"  [http] {label}: " + ", ".join(f"{k} p50={v['p50_ms']}ms p95={v['p95_ms']}ms" for k, v in row.items()))
    (OUT / "benchmark.json").write_text(json.dumps(res, indent=1, default=str))
    state.set_stage("benchmark", "done", articles=n_art)
