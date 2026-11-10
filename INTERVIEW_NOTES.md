# Interview notes: the 15 hardest questions, with concise answers

Measured numbers are in [reports/results.md](reports/results.md). Quote them from there; never from memory.

**1. Why not just ask an LLM to convert the whole app?**
Its output can't be trusted on facts. A parser gives every model, field and call site exactly, with line numbers,
and a checksum tells you exactly whether data was lost. I use the LLM only where judgment is required (schema
trade-offs, rewriting business logic, repairing failures) and surround it with deterministic gates. MongoDB's AMP
uses the same split: agents for code, deterministic tooling to move and validate records.

**2. How do you know the MongoDB version behaves the same?**
Three independent signals:
1. a 155-step golden master recorded from the original before any transformation, then hash-frozen;
2. the official RealWorld Hurl suite compared *per request entry* with the original's results (the same passes and
   the same failures);
3. a differential replay of about 90 requests against both backends on identical migrated data.
Data correctness is a separate matter, covered by the three checks.

**3. The original fails half the official suite. Why didn't you fix those bugs?**
A modernization should not change behaviour. If I fixed bugs during the migration, I couldn't tell a migration bug
from an intended change, and clients that depend on the current behaviour would break. The bugs are documented
(offset as a page index, a 500 without `tagList`, a 500 on a password-less update, inflated `articlesCount`) for a
separate, reviewed change.

**4. How do you stop the agent from cheating?**
"The agent must not grade its own homework":
- Tests are hashed into a manifest before Stage 4, and the hash is verified before every run.
- The agent only returns text, and a writer with a path allowlist decides what reaches disk. It can't touch tests,
  routes, error classes or even the models.
- The route table of the new app must equal the original's.
- The tests were recorded against the original app, before the agent existed. The agent sees failure diffs, never
  the test files.

**5. Walk me through the schema. Why embed tags but not favorites?**
Golden Rule: data accessed together is stored together, *if bounded*. Every article response includes `tagList`, and
it is small and refers to a catalogue table, so it is embedded. Favorites are one row per user per article, so they
are unbounded. They go in a junction collection with compound `_id {articleId, userId}`, plus a `favoritesCount`
counter, because every response shows the count but never the list.

**6. Follows are user→user. What goes wrong if you embed them?**
It's self-referential. Naive FK-embedding recurses without end; the AptMQL-Bench paper reports 3 databases failing
exactly this way. An array of follower ids would also be unbounded (a celebrity with millions of followers moves
toward the 16 MB limit and rewrites a huge document on every follow). I use a `followers` junction collection with
`_id {userId, followerId}`, indexes in both directions, and `followersCount` on the user. The lint rejects
self-referential embedding outright.

**7. Why didn't you denormalize the author name into articles (extended reference)?**
The LLM proposed it, and my self-review removed it. Each author block also needs `followersCount` and `following`,
which live with the user, so the users document must be read anyway. Copying `username` and `image` saves no read
and adds fan-out writes on every profile update. The draft also forgot `bio`, so it couldn't even produce the
response. Instead I batch-load the page's authors with one `$in` query.

**8. Why no unique index on username or slug?**
The original Postgres schema has no unique constraints; the app checks email and slug in code only, and not on update.
A unique index would turn the original's 201 on a duplicate username into a 500, which is an API change. I'd add
uniqueness later as a deliberate product change, after a de-duplication data check.

**9. How are the counters kept correct under concurrency?**
The junction insert is an upsert on the compound `_id`. The counter is incremented only if `upsertedCount === 1`
(or decremented only if `deletedCount === 1`), inside a transaction on the replica set. A double favorite is
idempotent and can't double count, and check 1 recomputes every counter in SQL after migration to verify it.

**10. Explain the three post-migration checks. Could they pass while data is wrong?**
1. SQL vs MQL per collection, with arrays and counts computed in SQL itself, so it doesn't share code with the migrator.
2. Every reference resolves (`$lookup`, then count the dangling ones).
3. Per-table counts plus per-column, order-independent SHA-256 checksums over (primary key, value).
To show they can fail, the unit tests corrupt MongoDB on purpose (a changed title, a deleted comment, a dangling
favorite, an extra tag) and assert that each check catches it. A check you have never seen fail is not evidence.

**11. What makes the migrator idempotent and resumable?**
The `_id` is derived from the primary key and every write is `ReplaceOne(upsert=True)`, so re-running gives
byte-identical collections (the test compares them). Batches are primary-key ranges computed up front, so they run in
parallel and each writes a checkpoint document; a resumed run skips completed batches. Counters continue the
Postgres sequences, so new ids match what Postgres would have assigned.

**12. Why Mongoose rather than the native driver?**
It keeps the rewritten controllers shaped like the originals (model objects, timestamps, schema defaults), which makes
the LLM's job and the review easier, and its `timestamps` matches Sequelize's. The overhead is reduced with `.lean()`.
For the hottest read paths I'd move to the native driver next.

**13. Your benchmark: isn't MongoDB winning just because you added indexes?**
That's why I measure four variants:
1. Postgres as the original schema has it (primary keys only);
2. Postgres with the *same* secondary indexes;
3. naive one-to-one MongoDB;
4. the designed MongoDB schema.
All four use the same batched query strategy, so the difference comes from the schema. Separately, I measure the
real apps over HTTP (that includes the original's N+1 queries). See results.md for which effects are real.

**14. How would you cut over production with zero downtime?**
Run the bulk migration (it's idempotent), then stream changes from Postgres (logical replication / Debezium) and apply
them as upserts by the same deterministic `_id`. Re-run the checks on samples, and run the differential test as a
shadow read. Flip reads first, then writes. After the flip, MongoDB change streams can feed a reverse sync to Postgres
so rollback stays possible, and they also keep downstream consumers in sync.

**15. What shard key would you choose?**
`articles` is the big collection, and its hot queries are latest-N, by author (feed) and by slug. A hashed `_id`
spreads writes but makes every list a scatter-gather. A ranged key `{authorId: 1, createdAt: -1}` matches the existing
index and targets the feed's `$in` per author, while the global "latest" list can come from a small materialized
recent-articles collection. `favorites` shards on `articleId` and `followers` on `userId`, the first field of each
compound `_id`. `comments` shards on `articleId`, so "comments of an article" stays on a single shard.

**Bonus: what was hardest?**
- **Reproducing ORM semantics.** Sequelize's `findAndCountAll` with a many-to-many include counts *joined rows*. The
  analyzer flags it as a rule, so the agent is told to reproduce it.
- **The free tier.** 8k tokens a minute forced one-file-per-call, caching, a deterministic choice of which file to
  repair, and model fallback.
- **Harness bugs, which I fixed in the harness rather than letting the agent work around them.** A readiness probe on
  a not-yet-converted endpoint, and a repair-target heuristic that was fooled by cascading failures.
