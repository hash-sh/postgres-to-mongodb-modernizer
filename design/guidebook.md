# Schema-design guidebook (input to the schema agent)

GOLDEN RULE: data that is accessed together is stored together, unless that would break a rule below.

R1 Embed vs reference. Embed child data when it is (a) read with the parent on hot endpoints, (b) bounded in count,
   and (c) rarely updated independently. Otherwise reference it (keep it in its own collection, keyed by the parent id).
R2 Bounded arrays only. An embedded array needs a known small upper bound (`maxItems`). Rows created by user
   activity (one row per user per parent: likes, follows, comments) are UNBOUNDED. Never embed them as arrays.
   Store them as their own collection, and keep a denormalized COUNTER on the parent if the count is read often.
R3 Many-to-many. Junction tables become either (a) a bounded array of keys/values on one side (e.g. a small tag
   list), or (b) a junction collection with a compound `_id` {a, b}, which makes writes idempotent, plus an index
   on the reverse direction.
R4 Self-references. Never embed a document of the same collection inside itself (it recurses without end). Model
   user→user relations as a junction collection.
R5 Extended reference. When a hot read shows a few fields of a parent (e.g. author name and avatar), you MAY copy
   those fields into the child ("extended reference"). Only do this when the copied fields change rarely, and every
   write to them updates the copies. Always also keep the parent id.
R6 Computed pattern / denormalized counters. Store counts that hot reads need (e.g. favorites count). The writes
   that change the underlying rows must also update the counter, in the same operation or transaction.
R7 16 MB limit. Estimated max document size must stay far below 16 MB (target < 1 MB).
R8 Recoverability. Every source table, row and column must remain recoverable from the target design (lossless).
R9 Identity. Use deterministic `_id`s derived from the source primary key (integers stay integers), so the
   migration is idempotent and every source row maps to exactly one document. Compound keys become `_id: {a, b}`.
R10 Indexes. Every query predicate and sort used by an endpoint needs a supporting index (equality fields first,
   then the sort field). Unique business keys get unique indexes.
R11 API contract. The HTTP responses must not change. Fields hidden by the old toJSON must stay hidden.
