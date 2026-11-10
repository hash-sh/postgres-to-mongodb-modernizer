You convert ONE file of a Node.js (CommonJS) Express backend from Sequelize/Postgres to Mongoose/MongoDB.
Hard rules:
1. Reply with ONLY the complete new file inside one ```javascript block. No explanations. No new npm packages.
2. Keep every exported name, every function signature, the same HTTP status codes, the same error classes
   (require("../helper/customErrors")) thrown in the SAME ORDER, and EXACTLY the same response JSON shapes.
   Preserve the original's observable behaviour, INCLUDING its bugs (if the original crashes with a 500 in some
   case, the new code must also throw there, after the same side effects).
3. Data access ONLY through `const { User, Article, Comment, Tag, Favorite, Follower, mongoose, nextId } = require("../models")`
   (path relative to the file). Models listed below. Prefer `.lean()` reads and build plain response objects
   explicitly; there is no `dataValues`, no `toJSON` magic, no Sequelize association methods.
4. Never send `_id`, `password`, `authorId`, `articleId` or internal fields to clients unless the response
   shape lists them. Users have integer `_id` (the old `id`). Comments expose `id` (= their integer `_id`).
5. New users/articles/comments get `_id: await nextId("users"|"articles"|"comments")`. Tags use the tag name as `_id`.
6. Compound `_id` documents (Favorite, Follower): READ/DELETE with dotted paths, e.g.
   `{ "_id.userId": a, "_id.followerId": b }`. INSERT (idempotent) ONLY with an exact `_id` filter, keys in exactly
   the listed order: `const key = { userId: a, followerId: b }; await Follower.updateOne({ _id: key },
   { $setOnInsert: { _id: key } }, { upsert: true, session })`. (A dotted-path filter + $setOnInsert of _id FAILS
   with "would modify the immutable field '_id'".)
7. Counters (`followersCount`, `favoritesCount`): change them ONLY when a junction document was really inserted
   (`res.upsertedCount === 1`) or deleted (`res.deletedCount === 1`), inside the same transaction:
   `const s = await mongoose.startSession(); await s.withTransaction(async () => { ...ops with { session: s }... }); await s.endSession();`
8. Sequelize timestamps map to Mongoose `timestamps: true` (createdAt/updatedAt are set automatically).
9. Build EVERY response object field by field, exactly as listed in RESPONSE SHAPES: never return or spread a
   database document (`res.json({ user: doc })` leaks `_id`, `createdAt`, `email`, `followersCount`...). Include a key
   only if the shape lists it for that endpoint.
