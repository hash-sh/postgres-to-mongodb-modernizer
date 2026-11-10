# DESIGN: mini-AMP, an AI-assisted Postgres → MongoDB application modernizer

This document explains how the system is built and why. Decisions taken along the way are logged in
[DECISIONS.md](DECISIONS.md); measured results are in [reports/results.md](reports/results.md).

## 1. The core principle: agents only where judgment is needed

A modernization has two kinds of work:

| Kind | Examples here | Who does it | Why |
|---|---|---|---|
| **Facts** | which models, fields and associations exist; which line calls `findAndCountAll`; how many rows were migrated; whether two JSON responses are equal | **Deterministic code** | A fact must be exactly right and reproducible. A parser cannot hallucinate a field, and a checksum cannot be "mostly equal". |
| **Judgment** | embed or reference? how to express `getFavorites({include, where})` in Mongoose? why does this test fail and how do I fix it? | **LLM agents** | No rule-based tool translates arbitrary business logic. This is where an LLM adds value. |

The rule: **an LLM never decides whether data was lost or whether behaviour matches.** It proposes; deterministic
code measures. This mirrors MongoDB's own description of AMP, which combines agents with deterministic tooling;
in its Devin integration the agent rewrites business logic and data-access code while deterministic tooling moves
and validates every record.

```
 deterministic ─────────────── LLM (judgment) ────────────── deterministic
 Stage 1 baseline + freeze      Stage 3 schema design          Stage 3 lint (gate)
 Stage 2 parser inventory       Stage 4 code transformation    Stage 4 frozen tests (gate)
                                        repair loop            Stage 5 migrate + 3 checks + differential
                                        optimization pass      Stage 6 benchmark + report
```

## 2. Pipeline (a plain state machine)

`python -m pipeline all` runs these stages in order. State is saved to `state/pipeline_state.json`
**atomically after every step** (whole stages, and individual chunks and iterations inside Stage 4), so a crash or
a rate-limit pause resumes where it stopped. No agent framework is used; each stage is one Python module in
`pipeline/stages/`.

1. **baseline**: start the original app on Postgres, run the official Hurl suite twice, record the 155-step golden
   master twice (it must be identical), then hash-freeze all tests.
2. **analysis**: `analyzer/analyze.js` (Babel AST) plus Postgres catalog introspection produce `inventory.json`,
   `access_patterns.json` and `chunks.json`.
3. **schema**: the LLM writes the conceptual and then the logical design in a DSL, followed by the lint/repair loop, an
   independent critic and the self-review gate. Outputs are `schema.json` and the naive one-to-one `schema_naive.json`.
4. **transform**: code generation of the Mongoose models, then per slice: LLM generation → `node --check` → frozen
   tests → repair (cap 5) → optimization pass.
5. **migrate**: seed → batched, parallel, idempotent, resumable migration → three checks → frozen tests and the
   official suite on MongoDB → differential replay against both backends.
6. **benchmark** and **report**.

## 3. Anti-cheating: the agent must not grade its own homework

If the same agent that writes the code can also edit the tests, "all tests pass" means nothing: the cheapest way to
make a test pass is to change the test. Three mechanisms, all in deterministic code (`pipeline/guard.py`), prevent it:

1. **Frozen tests.** Before any transformation, every file in `tests/official` and `tests/characterization` (including
   the golden master) is hashed into `tests/MANIFEST.sha256`, and the manifest's own hash goes into pipeline state.
   `verify_tests()` runs before *every* test run and raises on any change, so tampering cannot go unnoticed.
2. **Write allowlist.** The agent never touches the filesystem. It returns text, and `SafeWriter` writes it only if
   the path is in the data-layer allowlist (`controllers/*.js`, `helper/helpers.js`, `middleware/authentication.js`,
   `routes/tags.js`, `index.js`). Path traversal, symlinks, tests, routes, error classes and `package.json` are rejected.
   Even the Mongoose models are outside the allowlist: they are generated from the reviewed schema, so a schema problem
   has to go back through Stage 3's gate.
3. **API-contract check.** The same analyzer extracts the `(METHOD, path)` route table of the new app, which must equal
   the original's 21 routes before every test run.

**The tests and the transformation agent are separated** by construction. The tests were written and recorded
against the original before the agent existed. The agent sees only read-only *derived* information: response shapes
(key structure), the analyzer's semantic notes, and the failure diffs of its own attempts. It never sees the test
files or their expected values.

## 4. What "behaves the same" means (and why it's not "passes more tests")

The original app passes only 72 of 157 official request entries, because it predates the current spec and has bugs:
`offset` is a page index, a missing `tagList` causes a 500, a password-less `PUT /api/user` returns 500, and
`articlesCount` counts joined rows. The brief says the API contract must not change, so the rewrite must reproduce
all of that. Acceptance therefore means:

- **characterization:** every one of the 155 steps equals the golden master, and
- **official suite:** every request entry has the **same** pass/fail result as on the original, and
- **differential:** the same request sequence on identical migrated data gives identical normalized responses.

Normalization is minimal and justified. Tokens contain `iat`. Timestamps of rows created *during* the run differ
(migrated timestamps are compared strictly). `tagList`/`tags` order is unspecified because the original has no ORDER BY.
The text of a 500 error is a leaked JS exception string, but the status code is kept.

## 5. Schema decisions

The full reasoning is in [design/schema.md](design/schema.md) and [design/self_review.md](design/self_review.md).
The access patterns, weighted by assumed read frequency, drive everything: article lists, single articles and
comments dominate, and writes are rare.

| Relationship | Decision | Rule |
|---|---|---|
| Article–Tag (TagList, N:M) | **embed** `tagList: [String]` (maxItems 20) | always read with the article; bounded; Tags is a catalogue |
| Article–User favorites (N:M) | **junction collection** `favorites`, `_id: {articleId, userId}` + **counter** `articles.favoritesCount` | unbounded (one row per user) |
| User–User follows (N:M, self-ref) | **junction collection** `followers`, `_id: {userId, followerId}` + **counter** `users.followersCount` | self-reference: embedding would recurse; unbounded |
| Article–Comment (1:N) | **reference**: `comments` collection with `articleId` index | unbounded |
| Article/Comment → author (N:1) | **reference** (`authorId`), batch-loaded with `$in` | the response needs `followersCount`/`following` anyway, so copying fields would only add fan-out writes |
| ids | `_id` = Postgres PK (integers); a `counters` collection continues each Postgres sequence | deterministic, idempotent migration; comment ids are in the API |
| uniqueness | **no** unique indexes on username, email or slug | the original has none; a unique index would change 201 into 500 |

### Failure modes handled explicitly (from the AptMQL-Bench paper)
- **Self-referential recursion:** naive FK-embedding failed on 3 of the paper's databases this way. Follows
  (user→user) are a junction collection. Lint rule R4 rejects embedding a table into itself, or a junction with two
  FKs to the parent.
- **16 MB BSON limit:** another 3 databases failed this way. Lint rule R7 estimates the maximum document size from
  Postgres `pg_column_size` multiplied by `maxItems` (the final designed documents are a few KB).
- **Unbounded arrays:** rule R2 rejects embedding rows that are produced by user activity (favorites, follows,
  comments) and keeps a counter instead.
- **Silent row loss:** check 3 compares per-table counts and per-column checksums, and check 2 finds dangling references.

## 6. Validation (100% deterministic)
- **Check 1:** for each collection, a SQL query (which computes `array_agg` and `count` itself) and an MQL
  projection must agree row by row. This is independent of the migrator's own document builder.
- **Check 2:** every reference (FK-derived fields, compound-`_id` parts, embedded tag names) must resolve
  (`$lookup` → `$size: 0` = dangling).
- **Check 3:** per source table, the row count plus an order-independent SHA-256 checksum per column over
  (primary key, value). Each check is unit-tested against deliberately corrupted data, to show it can fail.
- **Behaviour:** frozen tests, the official suite per entry, and the differential replay.

## 7. Living with a free-tier LLM
Groq's free tier allows 8,000 tokens per minute per model. To work within it: one file per call; a cache on disk (a
re-run costs 0 tokens); a client-side limiter per model; a hard token budget that triggers a clean pause and resume;
and automatic fallback to another model's separate daily quota. The repair target is chosen deterministically: the
first failing step of each scenario is weighted as the probable root cause, and stack traces from the app's own files
take priority.

## 8. How this relates to MongoDB AMP and AptMQL-Bench
- **AMP** (MongoDB's Application Modernization Platform) pairs AI agents with deterministic tooling and testing. Agents
  rewrite application and data-access code, while tooling moves and validates every record. This project is a
  miniature version of that split, for one app.
- **AptMQL-Bench** (MongoDB Research, Oct 2026). Ideas borrowed:
  1. access-pattern-driven design with the Golden Rule;
  2. conceptual → logical design with a review gate (automated here);
  3. the three post-migration checks;
  4. translate, revise until it matches, then an optimization pass that removes relational-shaped access;
  5. explicit handling of self-reference, the 16 MB limit and unbounded arrays.
- **Difference:** the paper translates individual SQL queries to MQL and checks result sets. Here the unit is an
  application's data layer, so equivalence is checked at the HTTP API level.

## 9. What I would do next
- **Zero-downtime cutover with change streams.** Run the bulk migration (idempotent, so it can be repeated), then
  capture Postgres changes (logical replication or Debezium → Kafka) and apply them as upserts by the same
  deterministic `_id`. Re-run the three checks on a live sample, dual-read the differential test in shadow mode, flip
  reads, then flip writes. MongoDB **change streams** on the new cluster can feed a reverse sync to Postgres while
  rollback must stay possible. (Change streams *read* changes from MongoDB; they are the tool for the reverse
  direction and for keeping downstream systems in sync after the cutover.)
- **Sharding key.** `articles` is the large collection. Hot queries are "latest N", "latest N by author" (feed) and
  "by slug". A hashed `_id` key spreads writes but turns "latest N" into a scatter-gather. `{authorId: "hashed"}`
  would make the feed's `$in` targeted per author but leaves the global list scatter-gather. A good compromise is a
  ranged key `{authorId: 1, createdAt: -1}`, which matches the existing index; the global "latest" list can be served
  from a small capped/materialized "recent articles" collection. `favorites` and `followers` shard on their compound
  `_id`'s first field (`articleId` / `userId`), which keeps "favorites of this article" and "followers of this user"
  on a single shard. `comments` shards on `articleId`.
- Use the native driver on hot read paths; add schema validation (`$jsonSchema`) generated from the same DSL; and
  measure access patterns from real traffic instead of assuming them (the paper names assumed access patterns as a
  limitation too).
