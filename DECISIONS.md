# Decisions log

Every significant decision made autonomously while building this project: what was chosen, why, and the
alternatives considered. They are in roughly chronological order.

---

## Environment and tooling

### D1. LLM provider: Groq free tier (`openai/gpt-oss-120b`), with no Anthropic key
- **Why:** this was the only key provided. Running on a free tier became a design constraint: 8,000 tokens per minute
  per model (read from Groq's `x-ratelimit-*` headers with one 16-token probe call) and about 1,000 requests a day.
- **Model choice:** `gpt-oss-120b` was the strongest code model listed by `/models`. In the AptMQL-Bench table it
  scores 44.5 / 62.5 Soft-EX, so it is mid-pack and expected to need repair loops. `qwen/qwen3.8-27b` is the
  **critic** (a different model, so the designer is not reviewing itself) and is also the first fallback if
  `gpt-oss-120b` reaches its daily limit. Groq budgets each model separately.
- **Alternatives:** Anthropic Claude (not available), a local Ollama model (too weak and slow on CPU for code repair).

### D2. Free-tier-aware LLM client (`pipeline/llm.py`)
- On-disk cache keyed by the SHA-256 of the full request, so resuming or re-running costs 0 tokens.
- A client-side sliding-window limiter per model that waits *before* Groq returns a 429.
- A hard per-run token cap (`LLM_MAX_TOKENS_PER_RUN`, raised from 150k to 250k once Stage 4's size was known).
  Reaching the cap raises `BudgetExhausted`, the state is saved, and the pipeline exits cleanly so `python -m pipeline all` resumes.
- **Alternatives:** an agent framework such as LangChain. Rejected because the brief asks for a plain, explainable state
  machine, and the cache, budget and rate-limit logic is about 150 lines.

### D3. Official test suite: the current gothinkster/realworld **Hurl** suite, run through the Hurl Docker image
- The old Postman/Newman collection has been replaced upstream by `specs/api/hurl/*.hurl` (commit 2558884).
- Downloading the Hurl binary from GitHub was blocked by the sandbox's permission policy, so I used the official
  `ghcr.io/orange-opensource/hurl` image, pinned by digest (Hurl 8.1.0).
- **Metric:** results are counted per *request entry* with `--continue-on-error`, because "files passed" (1/13 on the
  original) is too coarse to say anything.

### D4. MongoDB 8.2 instead of 8.0
- `mongo:8.0` refuses to start on this kernel (Linux 7.0): *"Linux kernel versions 6.19 and newer has a known
  incompatibility"* (SERVER-121912). 8.2.12 starts fine. It runs as a single-node replica set `rs0`, initiated by the
  compose healthcheck, so transactions and change streams work.

### D5. Parser: `@babel/parser` + `@babel/traverse`
- The target is plain CommonJS JavaScript. Babel parses all modern syntax, and `traverse` gives **scope bindings**,
  which were needed to resolve options passed by variable (`findAndCountAll(searchOptions)`) back to their object literal.
- **Alternatives:** `ts-morph` (best for TypeScript and type information, heavier, and gives nothing extra for untyped
  JS); `tree-sitter` (fast and robust, but no scope analysis, so I would have had to write it).

---

## Stage 1: baseline and anti-cheating

### D6. The goal is "behaves the same as the original", not "passes more of the spec"
- The original passes 72 of 157 official request entries. Many failures are deliberate-looking quirks or bugs:
  `offset` is a page index, `PUT /api/user` without a password returns 500, a missing `tagList` returns 500,
  `articlesCount` counts joined rows, and validation errors return 422 where the spec says 401.
- The brief says the API contract must not change, so the MongoDB version must reproduce these too. Acceptance is
  **per-entry equality with the original** on the official suite, plus 100% equality with the golden master.
- **Alternative:** fixing bugs during the migration. Rejected because it mixes two changes and makes it impossible to
  tell migration bugs from behaviour changes. The bugs are listed for a separate follow-up.

### D7. Characterization tests as a golden master (`tests/characterization/`)
- 10 scenarios with 155 steps, covering every endpoint, error path and known quirk, and recorded from the original app.
- Each scenario starts on an **empty database** (reset before each one), so usernames, comment ids and tag lists are
  deterministic and scenarios are independent.
- The recording is made twice and must be identical, otherwise Stage 1 fails. On the very first attempt the
  `comments` scenario differed between the two recordings once. It could not be reproduced in 3 more full recordings
  or 8 isolated runs, and the final recording was identical twice. This is noted as an observed one-off; the
  likely cause is a race with `sequelize.sync({alter:true})` running right after app start.
- Normalization (in `pipeline/httpcheck.py`, documented in DESIGN.md) covers only tokens, timestamps created during
  the run, the order of tag arrays (no ORDER BY in the original) and the text of 500 errors (leaked JS exception strings).

### D8. Freeze the tests by hash before any transformation
- `tests/MANIFEST.sha256` lists the SHA-256 of every file in `tests/official` and `tests/characterization`. The
  manifest's own hash is stored in `state/pipeline_state.json`. `guard.verify_tests()` runs before **every** test
  run and fails loudly on any change, addition or removal.

---

## Stage 2: analysis

### D9. Instance calls are resolved by receiver name (`loggedUser.save()` → User)
- Static analysis cannot know the type of `article` in `article.destroy()`. A small explicit map from receiver
  variable names to models is deterministic and listed in `analysis.py`. **Alternative:** type inference. That is
  overkill for 6 call sites, and its output would be no more trustworthy here.

### D10. N+1 detection through the call graph
- A query is marked per-row if it sits in a loop, *or* sits in a helper (`appendFollowers`) that is called from a
  loop. This propagation is per endpoint. It shows that `GET /api/articles` issues 1 + 8·N queries, which is what
  motivates the later optimization pass.

### D11. Chunks are vertical slices, not one chunk per model
- Slice 1 is users/auth plus profiles/follows. Slice 2 is articles plus the feed. Slice 3 is comments, favorites and
  tags. Shared files (`helper/helpers.js`, `middleware/authentication.js`) go to the first slice that needs them, and
  later slices receive their converted source as context, so conventions such as the shape of `req.loggedUser` stay consistent.
- **Why not one chunk per model:** a model-only chunk cannot be tested through the API. A vertical slice can be, with
  the frozen tests that touch only its endpoints.

---

## Stage 3: schema

### D12. The LLM designs in a small, executable DSL (`pipeline/schema_dsl.py`)
- A free-form design document cannot be linted, migrated or validated. The DSL has collections from a source table,
  fields, bounded `embedArrays`, `extendedRefs`, `counters` and indexes. The migrator, the checks and the Mongoose
  codegen all read the same file.
- **Trade-off:** the DSL cannot express every MongoDB pattern (bucket, polymorphic), but it covers what this domain needs.

### D13. Lint rules are deterministic
- R2 *unbounded*: rows created by user activity (the table has its own serial id, or a foreign key to a non-catalogue
  table) must not be embedded.
- R4 *self-reference*: embedding a table into itself, or a junction with two foreign keys to the parent, is rejected.
- R7 *16 MB*: estimated from Postgres `pg_column_size` multiplied by `maxItems`.
- R8 *recoverability*: every column of every table must land somewhere.
- R10 *index coverage*: each predicate extracted from the call sites needs an index whose prefix matches.
- An index on an unknown field is also rejected. That check was added after the designer produced an index on a
  field it had moved elsewhere.

### D14. Review gate = lint + an independent critic model + a written self-review
- In place of the paper's human gate. The critic's "blocking" issues turned out to be mostly invalid (for example,
  it misread compound `_id`), so the designer is told to apply only issues that are valid under the guidebook, and the
  lint decides. The self-review (`design/self_review.md`) found real problems that both the lint and the critic
  missed. Its bullets go back to the designer as instructions; the reviewer never edits the JSON by hand.

### D15. Final schema choices (full reasoning in `design/self_review.md`)
- **Embed `tagList`** in articles: bounded, always read with the article, and it references a catalogue table.
- **Do not embed the author** (no extended reference). Every response's author block includes `followersCount` and
  `following`, so the users document must be read anyway, and copying fields would only add fan-out on profile updates.
- **Counters** `favoritesCount` and `followersCount`, because every article and profile response shows them. The
  unused `commentsCount` and `followingCount` were dropped.
- **Junction collections with a compound `_id`** for favorites and follows. Follows are self-referential and both are
  unbounded. Upserts on the compound `_id` are idempotent, and there is one index per lookup direction.
- **Non-unique** `username`, `email` and `slug` indexes. The original database has no unique constraints, and a
  unique index would turn the original's 201 on a duplicate username into a 500.
- **Integer `_id`s equal to the Postgres primary keys.** This makes the migration deterministic and idempotent, and
  comment ids are part of the API. A `counters` collection continues each Postgres sequence.

### D16. Naive baseline = one collection per table, same column names, no secondary indexes
- This is the paper's "one-to-one" conversion. It passes recoverability and fails all 14 index-coverage checks,
  which is the point of having it.

---

## Stage 4: transformation

### D17. Mongoose instead of the native driver
- The app is built around model objects (`User.findOne`, timestamps, schemas with defaults). Mongoose keeps the
  rewritten controllers structurally close to the originals, which makes the LLM's job and the review easier, and its
  `timestamps: true` matches Sequelize's behaviour. Indexes are declared next to the model.
- **Cost:** some overhead per query, reduced with `.lean()`. The native driver would be faster and is a reasonable
  next step for hot paths.

### D18. Models are generated, not written by the LLM (`pipeline/codegen.py`)
- They are a mechanical translation of the reviewed schema. Keeping them **outside the agent's write allowlist** means
  the agent cannot "fix" a failing test by changing the schema; a schema problem must go back to Stage 3.
- The agent may write only: `index.js`, `controllers/*.js`, `helper/helpers.js`, `middleware/authentication.js`,
  `routes/tags.js`. It cannot write routes, the error handler, error classes, JWT/bcrypt helpers, `package.json` or tests.

### D19. One file per LLM call
- A free-tier request must fit in 8k tokens including the answer. The largest file (`controllers/articles.js`, about
  2k tokens) plus context plus output fits. The repair target is chosen deterministically from failing routes and stack traces.

### D20. The agent is given read-only extracts of the spec, never the tests
- These are the response *shapes* from the frozen golden master (key structure only), the analyzer's semantic notes
  (for example the count-inflation rule) and the porting rules. The tests themselves, and their expected values,
  are never in the agent's context.

### D21. Harness bugs found while running Stage 4 (fixed in the harness, not by the agent)
- The readiness probe used `GET /api/tags`, a slice-3 endpoint that returns 500 until it is converted, so the
  first run timed out. One LLM repair iteration was spent on `index.js` because of this. It is logged and counted.
  The probe now uses `GET /`.
- If a registration step failed, the runner crashed on the missing token. It now sends a placeholder token so the
  step fails normally.

---

## Stage 5 and 6: migration, validation, benchmark

### D22. The checks are independent of the migrator
- Check 1 computes embedded arrays (`array_agg`) and counters (`count(*)`) **in SQL** and compares them with an MQL
  projection, so a bug in the migrator's Python document builder would be caught.
- Check 3 uses an order-independent sum of per-cell SHA-256 hashes over (primary key, column, value), computed
  separately on each side.
- The unit tests corrupt MongoDB on purpose (changed value, deleted row, dangling reference, extra tag) and assert
  that each check fails. A check that cannot fail proves nothing.

### D23. Migration parallelism by primary-key ranges, with checkpoints in Mongo
- Batch boundaries are computed up front (`row_number() % 2000`), so batches are independent and can run on a thread
  pool, and a resumed run skips checkpointed batches. Writes are `ReplaceOne(upsert)` keyed on the deterministic `_id`.
  Checkpoints live in `<db>__migration.checkpoints`.

### D24. Benchmark at two levels, plus Postgres with indexes
- **HTTP end-to-end** compares the real apps, which includes the original's N+1 queries.
- **Data-access level** runs the same batched strategy against Postgres, the naive Mongo schema and the designed Mongo
  schema, to isolate the effect of the schema.
- The original Sequelize schema has only primary-key indexes. Postgres is therefore also measured **with the same
  secondary indexes**, so the comparison does not credit MongoDB for what is really an indexing difference.
