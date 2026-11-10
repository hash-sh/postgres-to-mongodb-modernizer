"""Deterministic seeder for the ORIGINAL Postgres schema.

Same --articles value + same --seed  =>  byte-identical data, every run.
That matters because the differential test and the migration checks compare
Postgres with MongoDB, so we want reproducible inputs.

The tables themselves are created by the original app (`sequelize.sync`), so the
original backend must have been started once before seeding.

Usage:  python -m pipeline.seed --articles 1000          (validation dataset)
        python -m pipeline.seed --articles 100000        (benchmark dataset)
"""
import argparse
import io
import random
import time
from datetime import datetime, timedelta, timezone

import psycopg

from pipeline.config import settings

# bcrypt("password123", cost 10), computed once with the app's own bcrypt module.
# Every seeded user can log in with password123.
PASSWORD_HASH = "$2b$10$dHiq4XJnTSmHTsLh6kx73uzST5dhFVm28e5dvhhs7OBAS7G3F3uD6"
SEED_PASSWORD = "password123"

TAG_WORDS = [
    "mongodb", "postgres", "nodejs", "express", "react", "python", "golang", "rust",
    "databases", "sharding", "indexing", "devops", "docker", "kubernetes", "testing",
    "security", "design", "career", "ai", "llm", "migration", "performance", "linux",
    "cloud", "frontend", "backend", "javascript", "typescript", "graphql", "rest",
    "caching", "queues", "streaming", "analytics", "search", "mobile", "ux", "opensource",
    "startups", "productivity",
]
BODY_WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
              "incididunt ut labore et dolore magna aliqua données café naïve 数据 🚀").split()

BASE_TIME = datetime(2025, 1, 1, tzinfo=timezone.utc)


def slugify(s: str) -> str:
    # Mirrors backend/helper/helpers.js: trim, lowercase, replace /\W|_/g with "-"
    import re
    return re.sub(r"[^A-Za-z0-9]|_", "-", s.strip().lower())


def sizes(n_articles: int) -> dict:
    return {
        "users": max(20, n_articles // 5),
        "articles": n_articles,
        "tags": min(len(TAG_WORDS), max(10, n_articles // 50)),
    }


def generate(n_articles: int, seed: int = 42):
    """Yield (table, columns, rows) for every table, in FK-safe order."""
    rnd = random.Random(seed)
    sz = sizes(n_articles)
    U, A = sz["users"], sz["articles"]
    tags = TAG_WORDS[: sz["tags"]]

    users = []
    for i in range(1, U + 1):
        ts = BASE_TIME + timedelta(seconds=i)
        bio = None if i % 3 == 0 else f"I am user {i}. " + " ".join(rnd.choices(BODY_WORDS, k=6))
        image = None if i % 4 == 0 else f"https://img.example.com/u/{i}.png"
        users.append((i, f"user{i}@example.com", f"user{i}", bio, image, PASSWORD_HASH, ts, ts))
    yield "Users", ["id", "email", "username", "bio", "image", "password", "createdAt", "updatedAt"], users

    yield "Tags", ["name"], [(t,) for t in tags]

    # Skewed authorship: a few prolific authors, a long tail.
    author_weights = [1.0 / (i ** 0.8) for i in range(1, U + 1)]
    authors = rnd.choices(range(1, U + 1), weights=author_weights, k=A)
    articles, taglist = [], []
    for i in range(1, A + 1):
        # ASCII-only titles: JS \W works on UTF-16 code units, Python on code points.
        title = f"Article {i} {rnd.choice(TAG_WORDS)} {rnd.choice(BODY_WORDS[:18])}"
        ts = BASE_TIME + timedelta(days=1, seconds=37 * i)  # strictly increasing => stable ORDER BY createdAt
        body = " ".join(rnd.choices(BODY_WORDS, k=rnd.randint(20, 120)))
        articles.append((i, slugify(title), title, f"Description of article {i}", body, ts, ts, authors[i - 1]))
        for t in rnd.sample(tags, k=rnd.choice([0, 1, 2, 2, 3, 4])):
            taglist.append((i, t))
    yield "Articles", ["id", "slug", "title", "description", "body", "createdAt", "updatedAt", "userId"], articles
    yield "TagList", ["articleId", "tagName"], taglist

    # Favorites: popularity-skewed, (articleId, userId) unique.
    art_weights = [1.0 / (i ** 0.6) for i in range(1, A + 1)]
    fav = set()
    for u in range(1, U + 1):
        k = rnd.choice([0, 0, 1, 2, 3, 5, 8, 15])
        for a in rnd.choices(range(1, A + 1), weights=art_weights, k=k):
            fav.add((a, u))
    yield "Favorites", ["articleId", "userId"], sorted(fav)

    # Follows (self-referential): Followers(userId = followee, followerId = follower)
    fol = set()
    for f in range(1, U + 1):
        k = rnd.choice([0, 1, 2, 3, 5, 10])
        for target in rnd.choices(range(1, U + 1), weights=author_weights, k=k):
            if target != f:
                fol.add((target, f))
    yield "Followers", ["userId", "followerId"], sorted(fol)

    comments, cid = [], 0
    for a in range(1, A + 1):
        for _ in range(rnd.choice([0, 0, 1, 2, 3, 6])):
            cid += 1
            ts = BASE_TIME + timedelta(days=400, seconds=11 * cid)
            body = "Comment: " + " ".join(rnd.choices(BODY_WORDS, k=rnd.randint(3, 25)))
            comments.append((cid, body, ts, ts, a, rnd.randint(1, U)))
    yield "Comments", ["id", "body", "createdAt", "updatedAt", "articleId", "userId"], comments


def _copy(cur, table, cols, rows):
    collist = ", ".join(f'"{c}"' for c in cols)
    with cur.copy(f'COPY "{table}" ({collist}) FROM STDIN') as cp:
        for r in rows:
            cp.write_row(r)


def seed(n_articles: int, seed_value: int = 42) -> dict:
    t0 = time.time()
    counts = {}
    with psycopg.connect(settings.pg_dsn) as conn, conn.cursor() as cur:
        cur.execute('TRUNCATE "Comments","Favorites","Followers","TagList","Articles","Tags","Users" RESTART IDENTITY CASCADE')
        for table, cols, rows in generate(n_articles, seed_value):
            _copy(cur, table, cols, rows)
            counts[table] = len(rows)
        for t in ("Users", "Articles", "Comments"):
            cur.execute(f'SELECT setval(pg_get_serial_sequence(\'"{t}"\', \'id\'), COALESCE((SELECT MAX(id) FROM "{t}"), 1))')
        conn.commit()
    counts["seconds"] = round(time.time() - t0, 2)
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    print(seed(a.articles, a.seed))
