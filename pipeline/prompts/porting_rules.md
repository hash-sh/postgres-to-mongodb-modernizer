You convert ONE file of a Node.js (CommonJS) Express backend from Sequelize/Postgres to Mongoose/MongoDB.
Hard rules:
1. Reply with ONLY the complete new file inside one ```javascript block. No explanations. No new npm packages.
2. Keep every exported name, every function signature, the same HTTP status codes, the same error classes
   (require("../helper/customErrors")) thrown in the SAME ORDER, and EXACTLY the same response JSON shapes.
   Preserve the original's OBSERVABLE HTTP behaviour (status codes + bodies), INCLUDING its bugs: where the original
   returns a 500, throw an Error at the same point (after the same side effects). NEVER keep Sequelize API calls
   (getX/addX/hasX/countX/dataValues/save on plain objects) - they do not exist any more; use the table below.
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
   only if the shape lists it for that endpoint. The helpers (appendFollowers/appendFavorites) need `_id` on the
   objects they decorate: decorate an INTERNAL object, then return e.g. `toArticleJSON(a)` that copies only the
   shape's keys (author: { username, bio, image, following, followersCount } - no _id/email/createdAt).
10. Sequelize association method -> MongoDB equivalent under THIS schema (a = article doc, u/me = user doc):
   user.getFollowing()        -> (await Follower.find({ "_id.followerId": me._id }).lean()).map(f => f._id.userId)  // ids of followed users
   profile.hasFollower(me)    -> !!(await Follower.exists({ "_id.userId": profile._id, "_id.followerId": me._id }))
   profile.countFollowers()   -> profile.followersCount  (stored counter on users)
   article.getAuthor()        -> await User.findById(a.authorId).lean()
   article.getTagList()       -> a.tagList  (embedded array)
   article.addTagList(tag)    -> Article.updateOne({ _id: a._id }, { $addToSet: { tagList: name } })
   article.hasUser(me)        -> !!(await Favorite.exists({ "_id.articleId": a._id, "_id.userId": me._id }))
   article.countUsers()       -> a.favoritesCount  (stored counter on articles)
   user.getFavorites(...)     -> favorite article ids: (await Favorite.find({ "_id.userId": u._id }).lean()).map(f => f._id.articleId)
   user.countFavorites()      -> await Favorite.countDocuments({ "_id.userId": u._id })
   article.getComments()      -> await Comment.find({ articleId: a._id }).sort({ _id: 1 }).lean()
   article.destroy()          -> delete the article AND (FK ON DELETE CASCADE) its comments and favorites: Article.deleteOne,
                                 Comment.deleteMany({ articleId }), Favorite.deleteMany({ "_id.articleId": a._id })
   Model.findAndCountAll({include...}) -> find + countDocuments/aggregate (see ORM BEHAVIOUR notes for the count semantics)
11. Every ARTICLE object in a response MUST be produced by `await articleResponse(articleDoc, loggedUser)` from
   require("../helper/helpers") (it returns exactly {slug,title,description,body,createdAt,updatedAt,tagList,
   favorited,favoritesCount,author:{username,bio,image,following,followersCount}}). Never build article JSON by hand.

