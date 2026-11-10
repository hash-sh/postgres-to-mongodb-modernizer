"""Differential test: replay the SAME request sequence against both backends on IDENTICAL seeded data.

Postgres is seeded deterministically, then migrated to MongoDB, so both backends start from the same data.
The sequence is derived (deterministically) from that data: real users, tags, slugs and follow graphs.
Responses are normalized (tokens; timestamps of rows created DURING the run; order of tag arrays)
and diffed. Timestamps of migrated data are compared STRICTLY - they must survive migration exactly.
"""
import psycopg

from pipeline import httpcheck
from pipeline.config import settings
from pipeline.seed import SEED_PASSWORD


def build_sequence():
    with psycopg.connect(settings.pg_dsn) as c:
        top_authors = [r[0] for r in c.execute('SELECT u.username FROM "Users" u JOIN "Articles" a ON a."userId"=u.id '
                                                'GROUP BY u.username ORDER BY count(*) DESC, u.username LIMIT 3')]
        followers = [r[0] for r in c.execute('SELECT u.username FROM "Users" u JOIN "Followers" f ON f."followerId"=u.id '
                                             'GROUP BY u.username ORDER BY count(*) DESC, u.username LIMIT 3')]
        favers = [r[0] for r in c.execute('SELECT u.username FROM "Users" u JOIN "Favorites" f ON f."userId"=u.id '
                                          'GROUP BY u.username ORDER BY count(*) DESC, u.username LIMIT 3')]
        lonely = c.execute('SELECT username FROM "Users" u WHERE NOT EXISTS (SELECT 1 FROM "Followers" f WHERE f."followerId"=u.id) '
                           'AND NOT EXISTS (SELECT 1 FROM "Favorites" v WHERE v."userId"=u.id) ORDER BY id LIMIT 1').fetchone()[0]
        tags = [r[0] for r in c.execute('SELECT "tagName" FROM "TagList" GROUP BY 1 ORDER BY count(*) DESC, 1')]
        slugs = [r[0] for r in c.execute('SELECT a.slug FROM "Articles" a LEFT JOIN "Comments" m ON m."articleId"=a.id '
                                         'GROUP BY a.id ORDER BY count(m.id) DESC, a.id LIMIT 4')]
        slugs += [r[0] for r in c.execute('SELECT slug FROM "Articles" ORDER BY id LIMIT 2')]
        own = c.execute('SELECT a.slug FROM "Articles" a JOIN "Users" u ON u.id=a."userId" WHERE u.username=%s '
                        'ORDER BY a.id LIMIT 1', [followers[0]]).fetchone()
        not_followed = c.execute('SELECT u.username FROM "Users" u WHERE u.username <> %s AND NOT EXISTS (SELECT 1 FROM "Followers" f '
                                 'JOIN "Users" v ON v.id=f."followerId" WHERE f."userId"=u.id AND v.username=%s) ORDER BY u.id LIMIT 1',
                                 [followers[0], followers[0]]).fetchone()[0]
    viewers = list(dict.fromkeys(followers[:2] + favers[:1] + [lonely]))
    steps = [{"name": f"login {v}", "method": "POST", "path": "/api/users/login", "save_token": v,
              "body": {"user": {"email": f"{v}@example.com", "password": SEED_PASSWORD}}} for v in viewers]

    def g(name, path, as_=None):
        steps.append({"name": name, "method": "GET", "path": path, **({"as": as_} if as_ else {})})

    for who in [None, viewers[0], lonely]:
        tag = who or "anon"
        g(f"list default [{tag}]", "/api/articles", who)
        for off in range(4):
            g(f"list limit 20 page {off} [{tag}]", f"/api/articles?limit=20&offset={off}", who)
    g("list deep page", "/api/articles?limit=10&offset=40")
    for t in tags[:3] + tags[-1:]:
        g(f"tag {t}", f"/api/articles?tag={t}&limit=10", viewers[0])
        g(f"tag {t} page 1", f"/api/articles?tag={t}&limit=5&offset=1")
    for a in top_authors:
        g(f"author {a}", f"/api/articles?author={a}&limit=10", viewers[1])
    g("author+tag", f"/api/articles?author={top_authors[0]}&tag={tags[0]}&limit=10")
    for f in favers:
        g(f"favorited {f}", f"/api/articles?favorited={f}&limit=10", viewers[0])
    g(f"favorited {lonely} (none)", f"/api/articles?favorited={lonely}")
    for v in viewers:
        g(f"feed {v}", "/api/articles/feed?limit=10", v)
        g(f"feed {v} page 1", "/api/articles/feed?limit=5&offset=1", v)
        g(f"current user {v}", "/api/user", v)
    for u in top_authors + followers[:1] + [lonely]:
        g(f"profile {u} anon", f"/api/profiles/{u}")
        g(f"profile {u} as {viewers[0]}", f"/api/profiles/{u}", viewers[0])
    for s in slugs:
        g(f"article {s}", f"/api/articles/{s}", viewers[0])
        g(f"comments {s}", f"/api/articles/{s}/comments", viewers[1])
    g("tags", "/api/tags")

    v0 = viewers[0]
    W = steps.append
    W({"name": "follow", "method": "POST", "path": f"/api/profiles/{not_followed}/follow", "as": v0})
    g("feed after follow", "/api/articles/feed?limit=10", v0)
    W({"name": "unfollow", "method": "DELETE", "path": f"/api/profiles/{top_authors[0]}/follow", "as": v0})
    g("profile after unfollow", f"/api/profiles/{top_authors[0]}", v0)
    W({"name": "favorite", "method": "POST", "path": f"/api/articles/{slugs[0]}/favorite", "as": lonely})
    W({"name": "favorite again", "method": "POST", "path": f"/api/articles/{slugs[0]}/favorite", "as": lonely})
    W({"name": "unfavorite other", "method": "DELETE", "path": f"/api/articles/{slugs[1]}/favorite", "as": favers[0]})
    g("favorited after", f"/api/articles?favorited={lonely}", lonely)
    W({"name": "comment", "method": "POST", "path": f"/api/articles/{slugs[0]}/comments", "as": lonely,
       "body": {"comment": {"body": "differential comment"}}, "capture": {"cid": "comment.id"}})
    g("comments after create", f"/api/articles/{slugs[0]}/comments", lonely)
    W({"name": "delete comment", "method": "DELETE", "path": f"/api/articles/{slugs[0]}/comments/{{cid}}", "as": lonely})
    W({"name": "create article", "method": "POST", "path": "/api/articles", "as": lonely,
       "body": {"article": {"title": "Differential Article", "description": "d", "body": "b", "tagList": [tags[0], "newtag", "xy"]}}})
    g("list after create", "/api/articles?limit=3")
    g("tags after create", "/api/tags")
    W({"name": "update article", "method": "PUT", "path": "/api/articles/differential-article", "as": lonely,
       "body": {"article": {"title": "Differential Article v2", "body": "b2"}}})
    if own:
        W({"name": "foreign update forbidden", "method": "PUT", "path": f"/api/articles/{own[0]}", "as": lonely,
           "body": {"article": {"body": "x"}}})
        W({"name": "author deletes own seeded article", "method": "DELETE", "path": f"/api/articles/{own[0]}", "as": followers[0]})
        g("deleted article gone", f"/api/articles/{own[0]}")
        g("its comments gone", f"/api/articles/{own[0]}/comments")
    W({"name": "update user", "method": "PUT", "path": "/api/user", "as": lonely,
       "body": {"user": {"bio": "new bio", "password": "newpass1"}}})
    g("profile after update", f"/api/profiles/{lonely}")
    g("final list", "/api/articles?limit=20", v0)
    g("final feed", "/api/articles/feed?limit=20", v0)
    return {"name": "differential", "steps": steps}


def _sort_unordered(body):
    """Comments come from `article.getComments()` WITHOUT ORDER BY in the original: their order is unspecified by
    SQL (Postgres may return any order, e.g. by join plan). For the secondary, order-insensitive comparison only,
    sort them by id. The strict comparison is always reported first."""
    if isinstance(body, dict) and isinstance(body.get("comments"), list):
        body = {**body, "comments": sorted(body["comments"], key=lambda c: c.get("id", 0) if isinstance(c, dict) else 0)}
    return body


def run(orig_url, mongo_url, scenario, ts_cutoff):
    a = httpcheck.run_scenario(orig_url, scenario, ts_cutoff)
    b = httpcheck.run_scenario(mongo_url, scenario, ts_cutoff)
    matched, mismatches = 0, []
    for x, y in zip(a, b):
        d = ([f"status {x['status']} vs {y['status']}"] if x["status"] != y["status"] else []) + httpcheck.diff(x["body"], y["body"])
        if d:
            mismatches.append({"step": x["step"], "request": x["request"], "diffs": d[:8]})
        else:
            matched += 1
    matched_set = sum(1 for x, y in zip(a, b) if x["status"] == y["status"]
                      and not httpcheck.diff(_sort_unordered(x["body"]), _sort_unordered(y["body"])))
    return {"requests": len(a), "matched": matched, "match_rate": round(matched / len(a), 4) if a else None,
            "matched_ignoring_unspecified_order": matched_set, "mismatches": mismatches,
            "original_statuses": [x["status"] for x in a]}
