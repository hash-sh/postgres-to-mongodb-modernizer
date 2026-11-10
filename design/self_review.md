# Self-review of the LLM-designed schema (automated review gate)

The paper puts a human review gate here. This project replaces it with three layers: (1) the deterministic lint
(`pipeline/schema_dsl.py`), (2) an independent critic model (`qwen/qwen3.8-27b`, not the designer), and (3) this
written review by the orchestrating agent (Claude), who checked the design against `analysis/access_patterns.json`.
The reviewer does not edit `schema.json` by hand. The bullets under **Required changes** are sent back to the
designer model as instructions, and the result must pass the lint again before Stage 4 may start.

## Draft reviewed
The designer's draft (lint-clean after 3 repair rounds plus one critic round) had:
- `articles`: `tagList` embedded (maxItems 20), `favoritesCount` and `commentsCount` counters, an extended
  reference `author` copying `[id, username, image]`, a plain `authorId` field holding the same FK, and indexes
  `{slug:1} unique`, `{createdAt:1}`, `{authorId:1,createdAt:1}`, `{author.id:1,createdAt:1}`, `{tagList:1}`.
- `comments`: an `author` extended reference, plus indexes `{articleId:1}`, `{articleId:1,createdAt:-1}`, `{author.id:1}`.
- `users`: `followersCount` and `followingCount` counters, `{username:1} unique`, `{email:1}`.
- `favorites` and `followers`: junction collections with compound `_id` and one index per direction. `tags`: `_id` = name.

## Findings, checked against the access patterns

1. **The `author` extended reference does not pay for itself.** Every author object in a response (article list,
   feed, single article, comments) also contains `followersCount` and `following`. These are per-viewer or
   frequently-changing values that live with the user and the `followers` collection, so the endpoint must read the
   user document anyway. Copying `username`/`image` therefore saves no read and adds fan-out updates
   (`PUT /api/user` would have to rewrite every article and comment by that user). The copy also omitted `bio`,
   which every response includes, so it could not even produce the response. **Decision:** store only the author id
   and batch-load the authors of a page with one `$in` query.
2. **The same FK was stored twice** (`authorId` and `author.id`), which means two copies to keep consistent and two
   indexes. Finding 1 removes this. A lint warning for a column mapped twice was added.
3. **Unused counters.** No endpoint returns `commentsCount` or `followingCount`. Counters cost a write on every
   comment or follow, so remove them (R6 says to store counts that hot reads *need*). Keep `favoritesCount` and
   `followersCount`, which appear in almost every response.
4. **Unique indexes would change API behavior (R11).** The original Postgres schema has no UNIQUE constraint on
   `username`, `email` or `slug`. The app checks email (on sign-up) and slug (on create only) in code. With a
   unique index on `username`, registering a duplicate username would fail with 500, but the original returns 201.
   `PUT /api/articles/:slug` can also produce a duplicate slug, which the original accepts. Make these indexes non-unique.
5. **Index shape.** The default list sorts by `createdAt` desc. The tag filter, author filter and feed all filter
   and then sort by `createdAt` desc, so the indexes should be `{tagList:1, createdAt:-1}` and
   `{authorId:1, createdAt:-1}` (the feed uses `authorId $in [...]`). Comments are only queried by `articleId`.
6. **Critic points.** Accepted: the redundant author index. Rejected: an index on `comments.userId` (no endpoint
   queries comments by user, and every index costs writes) and `{articleId, createdAt}` on comments (the original
   returns comments in insertion order, which `_id` order reproduces).
7. **Verified OK:** `tagList` is embedded (bounded, always read with the article, a catalogue FK, never
   self-referential). Favorites and follows are junction collections (unbounded, R2; follows are self-referential,
   R4) with compound `_id` for idempotent upserts and one index per lookup direction. `_id`s are deterministic from
   the PKs (comment ids stay integers, which the API exposes). The estimated max document size is far below 16 MB.

## Consequences for Stage 4 (given to the transformation agent)
- `favoritesCount` and `followersCount` must be updated in the same transaction as the junction insert/delete, and
  only when a row was actually inserted or deleted (favoriting twice must not count twice).
- `PUT /api/user` touches only `users`. Deleting an article must also delete its comments and favorites (the
  original cascades via FK).

## Required changes
- In collection articles: remove the extendedRefs entry "author"; keep the plain field authorId (from userId, type int); remove the index {"author.id":1,"createdAt":1}.
- In collection comments: remove the extendedRefs entry "author" and add a plain field authorId (from userId, type int).
- In collection articles: remove the counter commentsCount. In collection users: remove the counter followingCount.
- Set the articles indexes to exactly: {"slug":1} (NOT unique), {"createdAt":-1}, {"authorId":1,"createdAt":-1}, {"tagList":1,"createdAt":-1}.
- Set the comments indexes to exactly: {"articleId":1}.
- Set the users indexes to exactly: {"username":1} (NOT unique), {"email":1} (NOT unique). The original database has no unique constraints and the API behavior must not change.
