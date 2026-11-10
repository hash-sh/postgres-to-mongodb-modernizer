"""Characterization tests for the ORIGINAL Conduit backend.

These describe what the original app ACTUALLY does (including its bugs), so the
MongoDB rewrite can be held to "behaves the same". They are recorded against the
original app at Stage 1 (golden master) and then frozen + hashed before any
transformation starts. The transformation agent can never modify them.

Each scenario runs on an EMPTY database (reset before every scenario), so scenarios
are independent and every value (usernames, comment ids, tag lists) is deterministic.

slice: which conversion chunk must be done before this scenario can pass
   1 = users/auth + profiles/follows
   2 = articles + feed
   3 = comments + favorites + tags
"""


def reg(alias, name=None):
    name = name or alias
    return {"name": f"register {alias}", "method": "POST", "path": "/api/users", "save_token": alias,
            "body": {"user": {"username": name, "email": f"{name}@example.com", "password": f"pw-{name}"}}}


def art(alias, title, tags=("dragons", "training")):
    return {"name": f"{alias} creates '{title}'", "method": "POST", "path": "/api/articles", "as": alias,
            "body": {"article": {"title": title, "description": f"About {title}", "body": f"Body of {title}",
                                 "tagList": list(tags)}}}


def get(name, path, as_=None, **kw):
    return {"name": name, "method": "GET", "path": path, **({"as": as_} if as_ else {}), **kw}


SCENARIOS = [
    # ------------------------------------------------------------------ slice 1
    {"name": "auth_happy_path", "slice": 1, "steps": [
        reg("alice"),
        {"name": "login", "method": "POST", "path": "/api/users/login", "save_token": "alice2",
         "body": {"user": {"email": "alice@example.com", "password": "pw-alice"}}},
        get("current user", "/api/user", "alice"),
        get("current user with login token", "/api/user", "alice2"),
        {"name": "update bio+image+password", "method": "PUT", "path": "/api/user", "as": "alice",
         "body": {"user": {"bio": "I like dragons", "image": "https://i.example.com/a.png", "password": "newpw"}}},
        get("current user after update", "/api/user", "alice"),
        {"name": "login with new password", "method": "POST", "path": "/api/users/login",
         "body": {"user": {"email": "alice@example.com", "password": "newpw"}}},
        {"name": "login with old password fails", "method": "POST", "path": "/api/users/login",
         "body": {"user": {"email": "alice@example.com", "password": "pw-alice"}}},
    ]},
    {"name": "auth_errors", "slice": 1, "steps": [
        reg("bob"),
        {"name": "register duplicate email", "method": "POST", "path": "/api/users",
         "body": {"user": {"username": "bob2", "email": "bob@example.com", "password": "x"}}},
        {"name": "register missing username", "method": "POST", "path": "/api/users",
         "body": {"user": {"email": "c@example.com", "password": "x"}}},
        {"name": "register missing email", "method": "POST", "path": "/api/users",
         "body": {"user": {"username": "c", "password": "x"}}},
        {"name": "register missing password", "method": "POST", "path": "/api/users",
         "body": {"user": {"username": "c", "email": "c@example.com"}}},
        {"name": "login unknown email", "method": "POST", "path": "/api/users/login",
         "body": {"user": {"email": "nobody@example.com", "password": "x"}}},
        {"name": "login wrong password", "method": "POST", "path": "/api/users/login",
         "body": {"user": {"email": "bob@example.com", "password": "wrong"}}},
        get("current user without token", "/api/user"),
        get("current user with malformed header", "/api/user", auth_header="Token"),
        get("current user with garbage token", "/api/user", auth_header="Token not-a-jwt"),
        {"name": "update user without password (original bug: bcrypt(undefined))", "method": "PUT",
         "path": "/api/user", "as": "bob", "body": {"user": {"bio": "only bio"}}},
        get("bio unchanged after failed update", "/api/user", "bob"),
        {"name": "update user without token", "method": "PUT", "path": "/api/user",
         "body": {"user": {"bio": "x"}}},
    ]},
    {"name": "profiles_and_follows", "slice": 1, "steps": [
        reg("carol"), reg("dave"), reg("erin"),
        get("profile anonymous", "/api/profiles/carol"),
        get("profile as dave", "/api/profiles/carol", "dave"),
        get("unknown profile", "/api/profiles/nobody"),
        {"name": "dave follows carol", "method": "POST", "path": "/api/profiles/carol/follow", "as": "dave"},
        {"name": "dave follows carol again (idempotent)", "method": "POST", "path": "/api/profiles/carol/follow", "as": "dave"},
        {"name": "erin follows carol", "method": "POST", "path": "/api/profiles/carol/follow", "as": "erin"},
        get("carol as dave (following, 2 followers)", "/api/profiles/carol", "dave"),
        get("carol anonymous (following false)", "/api/profiles/carol"),
        get("dave as carol (not following back)", "/api/profiles/dave", "carol"),
        {"name": "carol follows herself", "method": "POST", "path": "/api/profiles/carol/follow", "as": "carol"},
        get("carol self-view", "/api/profiles/carol", "carol"),
        {"name": "dave unfollows carol", "method": "DELETE", "path": "/api/profiles/carol/follow", "as": "dave"},
        {"name": "dave unfollows carol again", "method": "DELETE", "path": "/api/profiles/carol/follow", "as": "dave"},
        get("carol as dave after unfollow", "/api/profiles/carol", "dave"),
        {"name": "follow without auth", "method": "POST", "path": "/api/profiles/carol/follow"},
        {"name": "follow unknown user", "method": "POST", "path": "/api/profiles/nobody/follow", "as": "dave"},
        {"name": "unfollow unknown user", "method": "DELETE", "path": "/api/profiles/nobody/follow", "as": "dave"},
    ]},
    {"name": "unknown_route", "slice": 1, "steps": [
        get("unknown api route", "/api/nothing-here"),
    ]},

    # ------------------------------------------------------------------ slice 2
    {"name": "article_crud", "slice": 2, "steps": [
        reg("frank"), reg("gina"),
        art("frank", "How to train your dragon", ("dragons", "training", "ok")),
        get("get article anonymous", "/api/articles/how-to-train-your-dragon"),
        get("get article as gina", "/api/articles/how-to-train-your-dragon", "gina"),
        get("get unknown article", "/api/articles/does-not-exist"),
        art("frank", "How to train your dragon", ("x",)),
        {"name": "create without auth", "method": "POST", "path": "/api/articles",
         "body": {"article": {"title": "T", "description": "D", "body": "B", "tagList": []}}},
        {"name": "create missing title", "method": "POST", "path": "/api/articles", "as": "frank",
         "body": {"article": {"description": "D", "body": "B", "tagList": []}}},
        {"name": "create missing description", "method": "POST", "path": "/api/articles", "as": "frank",
         "body": {"article": {"title": "T2", "body": "B", "tagList": []}}},
        {"name": "create missing body", "method": "POST", "path": "/api/articles", "as": "frank",
         "body": {"article": {"title": "T3", "description": "D", "tagList": []}}},
        {"name": "create without tagList (original bug: iterating undefined)", "method": "POST",
         "path": "/api/articles", "as": "frank",
         "body": {"article": {"title": "No Tags Here", "description": "D", "body": "B"}}},
        {"name": "gina cannot update frank's article", "method": "PUT", "path": "/api/articles/how-to-train-your-dragon",
         "as": "gina", "body": {"article": {"body": "hacked"}}},
        {"name": "update without auth", "method": "PUT", "path": "/api/articles/how-to-train-your-dragon",
         "body": {"article": {"body": "x"}}},
        {"name": "update unknown article", "method": "PUT", "path": "/api/articles/nope", "as": "frank",
         "body": {"article": {"body": "x"}}},
        {"name": "frank updates body", "method": "PUT", "path": "/api/articles/how-to-train-your-dragon",
         "as": "frank", "body": {"article": {"body": "New body", "description": "New description"}}},
        {"name": "frank updates title (slug changes)", "method": "PUT", "path": "/api/articles/how-to-train-your-dragon",
         "as": "frank", "body": {"article": {"title": "Dragons: The Sequel!"}}},
        get("old slug is gone", "/api/articles/how-to-train-your-dragon"),
        get("new slug works", "/api/articles/dragons--the-sequel-"),
        {"name": "gina cannot delete", "method": "DELETE", "path": "/api/articles/dragons--the-sequel-", "as": "gina"},
        {"name": "delete without auth", "method": "DELETE", "path": "/api/articles/dragons--the-sequel-"},
        {"name": "frank deletes", "method": "DELETE", "path": "/api/articles/dragons--the-sequel-", "as": "frank"},
        get("deleted article is gone", "/api/articles/dragons--the-sequel-"),
        {"name": "delete unknown", "method": "DELETE", "path": "/api/articles/dragons--the-sequel-", "as": "frank"},
    ]},
    {"name": "article_listing_filters_pagination", "slice": 2, "steps": [
        reg("hank"), reg("ivy"),
        art("hank", "Alpha", ("dragons", "training")),
        art("hank", "Beta", ("dragons",)),
        art("ivy", "Gamma", ()),
        art("ivy", "Delta", ("training", "cooking", "zz")),
        art("hank", "Epsilon", ("cooking",)),
        get("list default (limit 3)", "/api/articles"),
        get("list as ivy", "/api/articles", "ivy"),
        get("list limit 2 offset 0", "/api/articles?limit=2&offset=0"),
        get("list limit 2 offset 1 (offset is a page index)", "/api/articles?limit=2&offset=1"),
        get("list limit 2 offset 2", "/api/articles?limit=2&offset=2"),
        get("list limit 10", "/api/articles?limit=10"),
        get("filter by author hank", "/api/articles?author=hank&limit=10"),
        get("filter by author ivy", "/api/articles?author=ivy&limit=10"),
        get("filter by unknown author", "/api/articles?author=nobody"),
        get("filter by tag dragons", "/api/articles?tag=dragons&limit=10"),
        get("filter by tag cooking limit 1", "/api/articles?tag=cooking&limit=1"),
        get("filter by unknown tag", "/api/articles?tag=nope"),
        get("filter by author+tag", "/api/articles?author=hank&tag=cooking&limit=10"),
        get("filter favorited by ivy (none yet)", "/api/articles?favorited=ivy"),
        get("filter favorited by unknown user (original: 500)", "/api/articles?favorited=nobody"),
    ]},
    {"name": "feed", "slice": 2, "steps": [
        reg("jack"), reg("kim"), reg("leo"),
        art("kim", "Kim one", ("a1", "b22")),
        art("leo", "Leo one", ("dragons",)),
        art("kim", "Kim two", ()),
        get("feed without auth", "/api/articles/feed"),
        get("feed following nobody", "/api/articles/feed", "jack"),
        {"name": "jack follows kim", "method": "POST", "path": "/api/profiles/kim/follow", "as": "jack"},
        get("feed with kim", "/api/articles/feed", "jack"),
        {"name": "jack follows leo", "method": "POST", "path": "/api/profiles/leo/follow", "as": "jack"},
        get("feed with kim+leo", "/api/articles/feed?limit=10", "jack"),
        get("feed limit 1 offset 1", "/api/articles/feed?limit=1&offset=1", "jack"),
        get("article shows following=true for jack", "/api/articles/kim-one", "jack"),
        {"name": "jack unfollows kim", "method": "DELETE", "path": "/api/profiles/kim/follow", "as": "jack"},
        get("feed after unfollow", "/api/articles/feed?limit=10", "jack"),
    ]},

    # ------------------------------------------------------------------ slice 3
    {"name": "comments", "slice": 3, "steps": [
        reg("mia"), reg("ned"),
        art("mia", "Commented article", ("talk",)),
        get("no comments yet", "/api/articles/commented-article/comments"),
        {"name": "ned comments", "method": "POST", "path": "/api/articles/commented-article/comments", "as": "ned",
         "body": {"comment": {"body": "First!"}}, "capture": {"c1": "comment.id"}},
        {"name": "mia comments", "method": "POST", "path": "/api/articles/commented-article/comments", "as": "mia",
         "body": {"comment": {"body": "Thanks ned"}}, "capture": {"c2": "comment.id"}},
        get("list comments anonymous", "/api/articles/commented-article/comments"),
        {"name": "mia follows ned", "method": "POST", "path": "/api/profiles/ned/follow", "as": "mia"},
        get("list comments as mia", "/api/articles/commented-article/comments", "mia"),
        {"name": "comment without auth", "method": "POST", "path": "/api/articles/commented-article/comments",
         "body": {"comment": {"body": "x"}}},
        {"name": "comment with empty body", "method": "POST", "path": "/api/articles/commented-article/comments",
         "as": "ned", "body": {"comment": {"body": ""}}},
        {"name": "comment on unknown article", "method": "POST", "path": "/api/articles/nope/comments", "as": "ned",
         "body": {"comment": {"body": "x"}}},
        get("comments of unknown article", "/api/articles/nope/comments"),
        {"name": "mia cannot delete ned's comment", "method": "DELETE",
         "path": "/api/articles/commented-article/comments/{c1}", "as": "mia"},
        {"name": "delete comment without auth", "method": "DELETE", "path": "/api/articles/commented-article/comments/{c1}"},
        {"name": "ned deletes his comment", "method": "DELETE", "path": "/api/articles/commented-article/comments/{c1}", "as": "ned"},
        {"name": "delete it again", "method": "DELETE", "path": "/api/articles/commented-article/comments/{c1}", "as": "ned"},
        get("one comment left", "/api/articles/commented-article/comments"),
        {"name": "mia deletes the article (cascade)", "method": "DELETE", "path": "/api/articles/commented-article", "as": "mia"},
        get("comments after article deleted", "/api/articles/commented-article/comments"),
        {"name": "mia's comment id after cascade", "method": "DELETE",
         "path": "/api/articles/commented-article/comments/{c2}", "as": "mia"},
    ]},
    {"name": "favorites", "slice": 3, "steps": [
        reg("olga"), reg("pete"), reg("quinn"),
        art("olga", "Fav me", ("love",)),
        art("olga", "Not me", ()),
        {"name": "pete favorites", "method": "POST", "path": "/api/articles/fav-me/favorite", "as": "pete"},
        {"name": "pete favorites again (idempotent)", "method": "POST", "path": "/api/articles/fav-me/favorite", "as": "pete"},
        {"name": "quinn favorites", "method": "POST", "path": "/api/articles/fav-me/favorite", "as": "quinn"},
        get("article as pete", "/api/articles/fav-me", "pete"),
        get("article anonymous", "/api/articles/fav-me"),
        get("list as quinn", "/api/articles?limit=10", "quinn"),
        get("favorited by pete", "/api/articles?favorited=pete&limit=10"),
        get("favorited by pete as olga", "/api/articles?favorited=pete", "olga"),
        get("favorited by olga (none)", "/api/articles?favorited=olga"),
        {"name": "favorite without auth", "method": "POST", "path": "/api/articles/fav-me/favorite"},
        {"name": "favorite unknown article", "method": "POST", "path": "/api/articles/nope/favorite", "as": "pete"},
        {"name": "pete unfavorites", "method": "DELETE", "path": "/api/articles/fav-me/favorite", "as": "pete"},
        {"name": "pete unfavorites again", "method": "DELETE", "path": "/api/articles/fav-me/favorite", "as": "pete"},
        get("article after unfavorite", "/api/articles/fav-me", "pete"),
        get("favorited by pete after unfavorite", "/api/articles?favorited=pete"),
        {"name": "olga deletes favorited article", "method": "DELETE", "path": "/api/articles/fav-me", "as": "olga"},
        get("favorited by quinn after delete", "/api/articles?favorited=quinn"),
    ]},
    {"name": "tags", "slice": 3, "steps": [
        get("tags on empty db", "/api/tags"),
        reg("rita"),
        art("rita", "Tagged one", ("zebra", "apple", "ab", "mango")),
        art("rita", "Tagged two", ("apple", "kiwi")),
        get("tags after two articles (short tag 'ab' not created)", "/api/tags"),
        get("short tag is still on the article response list", "/api/articles/tagged-one"),
        {"name": "delete article keeps tags", "method": "DELETE", "path": "/api/articles/tagged-one", "as": "rita"},
        get("tags after delete", "/api/tags"),
    ]},
]
