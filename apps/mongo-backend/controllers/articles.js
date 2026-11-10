const {
  AlreadyTakenError,
  FieldRequiredError,
  ForbiddenError,
  NotFoundError,
  UnauthorizedError,
} = require("../helper/customErrors");
const { appendFollowers, appendFavorites, appendTagList, slugify, articleResponse } = require("../helper/helpers");
const { User, Article, Tag, Favorite, Follower, mongoose, nextId, Comment } = require("../models");

//? All Articles - by Author/by Tag/Favorited by user
const allArticles = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    const {
      author,
      tag,
      favorited,
      limit = 3,
      offset = 0,
    } = req.query;

    const lim = parseInt(limit);
    const skip = offset * lim;

    let match = {};

    // author filter (INNER JOIN semantics: if author not found, no results)
    if (author) {
      const authorUser = await User.findOne({ username: author }).lean();
      if (!authorUser) {
        return res.json({ articles: [], articlesCount: 0 });
      }
      match.authorId = authorUser._id;
    }

    // tag filter (INNER JOIN semantics: if tag matches nothing, no results)
    if (tag) {
      match.tagList = tag;
    }

    let articlesDocs = [];
    let articlesCount = 0;

    if (favorited) {
      // find user who favorited
      const user = await User.findOne({ username: favorited }).lean();
      // this will throw TypeError if user is null, matching original behaviour
      const favIds = (
        await Favorite.find({ "_id.userId": user._id }).lean()
      ).map((f) => f._id.articleId);

      if (favIds.length) {
        match._id = { $in: favIds };
      } else {
        match._id = { $in: [] };
      }

      articlesDocs = await Article.find(match)
        .sort({ createdAt: -1 })
        .skip(skip)
        .limit(lim)
        .lean();

      // countFavorites() in original counts all favorites of the user, not filtered by author/tag
      articlesCount = await Favorite.countDocuments({ "_id.userId": user._id });
    } else {
      articlesDocs = await Article.find(match)
        .sort({ createdAt: -1 })
        .skip(skip)
        .limit(lim)
        .lean();

      // Reproduce Sequelize findAndCountAll count semantics with LEFT JOIN on tagList:
      // Each article contributes max(1, tagCount) to the count.
      // If tag filter is active, it's an INNER JOIN, so only articles with that tag count 1 each.
      if (tag) {
        articlesCount = await Article.countDocuments(match);
      } else {
        // Aggregate to compute sum of max(1, tagList.length) for matching articles
        const countResult = await Article.aggregate([
          { $match: match },
          {
            $project: {
              contribution: {
                $max: [1, { $size: { $ifNull: ["$tagList", []] } }],
              },
            },
          },
          { $group: { _id: null, total: { $sum: "$contribution" } } },
        ]);
        articlesCount = countResult.length > 0 ? countResult[0].total : 0;
      }
    }

    const articles = [];
    if (articlesDocs.length > 0) {
      const articleIds = articlesDocs.map((a) => a._id);
      const authorIds = [...new Set(articlesDocs.map((a) => a.authorId))];

      // Batch load authors
      const authors = await User.find({ _id: { $in: authorIds } }).lean();
      const authorMap = new Map(authors.map((u) => [u._id, u]));

      // Batch load viewer's follows and favorites for these articles
      let followingSet = new Set();
      let favoritedSet = new Set();

      if (loggedUser) {
        const follows = await Follower.find({
          "_id.userId": { $in: authorIds },
          "_id.followerId": loggedUser._id,
        }).lean();
        followingSet = new Set(follows.map((f) => f._id.userId));

        const favorites = await Favorite.find({
          "_id.articleId": { $in: articleIds },
          "_id.userId": loggedUser._id,
        }).lean();
        favoritedSet = new Set(favorites.map((f) => f._id.articleId));
      }

      for (const doc of articlesDocs) {
        const authorDoc = authorMap.get(doc.authorId);

        const following = loggedUser ? followingSet.has(doc.authorId) : false;
        const favorited = loggedUser ? favoritedSet.has(doc._id) : false;

        articles.push({
          slug: doc.slug,
          title: doc.title,
          description: doc.description,
          body: doc.body,
          createdAt: doc.createdAt,
          updatedAt: doc.updatedAt,
          tagList: Array.isArray(doc.tagList) ? [...doc.tagList] : [],
          favorited,
          favoritesCount: doc.favoritesCount ?? 0,
          author: {
            username: authorDoc ? authorDoc.username : null,
            bio: authorDoc ? authorDoc.bio : null,
            image: authorDoc ? authorDoc.image : null,
            following,
            followersCount: authorDoc ? (authorDoc.followersCount ?? 0) : 0,
          },
        });
      }
    }

    res.json({ articles, articlesCount });
  } catch (error) {
    next(error);
  }
};

//* Create Article
const createArticle = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { title, description, body, tagList } = req.body.article;
    if (!title) throw new FieldRequiredError("A title");
    if (!description) throw new FieldRequiredError("A description");
    if (!body) throw new FieldRequiredError("An article body");

    const slug = slugify(title);
    const slugInDB = await Article.findOne({ slug: slug }).lean();
    if (slugInDB) throw new AlreadyTakenError("Title");

    const articleId = await nextId("articles");
    const article = await Article.create({
      _id: articleId,
      slug: slug,
      title: title,
      description: description,
      body: body,
      // MANUAL FIX (engineer, not LLM - DECISIONS D42): like the original, the article is saved WITHOUT an author
      // and the author is set only after the tag loop. If the loop throws (no tagList => 500 in the original), the
      // article stays orphaned and never appears in anyone's feed - exactly as on Postgres.
      authorId: null,
      tagList: [],
      favoritesCount: 0
    });

    for (const tag of tagList) {
      const trimmedTag = tag.trim();
      const tagInDB = await Tag.findById(trimmedTag).lean();

      if (tagInDB) {
        await Article.updateOne({ _id: article._id }, { $addToSet: { tagList: trimmedTag } });
      } else if (tag.length > 2) { // MANUAL FIX: the original tests the UNtrimmed length
        await Tag.create({ _id: trimmedTag });
        await Article.updateOne({ _id: article._id }, { $addToSet: { tagList: trimmedTag } });
      }
    }

    await Article.updateOne({ _id: article._id }, { $set: { authorId: loggedUser._id } }); // = article.setAuthor()

    const freshArticle = await Article.findById(article._id).lean();
    const articleJSON = await articleResponse(freshArticle, loggedUser);
    articleJSON.tagList = tagList;

    res.status(201).json({ article: articleJSON });
  } catch (error) {
    next(error);
  }
};

//* Feed
const articlesFeed = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { limit = 3, offset = 0 } = req.query;
    const followedUserIds = (
      await Follower.find({ "_id.followerId": loggedUser._id }).lean()
    ).map((f) => f._id.userId);

    const articles = await Article.find({
      authorId: { $in: followedUserIds },
    })
      .sort({ createdAt: -1 })
      .skip(parseInt(offset) * parseInt(limit))
      .limit(parseInt(limit))
      .lean();

    const articlesCount = (
      await Article.aggregate([
        { $match: { authorId: { $in: followedUserIds } } },
        {
          $group: {
            _id: null,
            count: {
              $sum: {
                $max: [
                  1,
                  { $size: { $ifNull: ["$tagList", []] } },
                ],
              },
            },
          },
        },
      ])
    )[0]?.count ?? 0;

    const responseArticles = [];
    for (const article of articles) {
      responseArticles.push(await articleResponse(article, loggedUser));
    }

    res.json({ articles: responseArticles, articlesCount });
  } catch (error) {
    next(error);
  }
};

// Single Article by slug
const singleArticle = async (req, res, next) => {
  try {
    const { loggedUser } = req;

    const { slug } = req.params;
    const article = await Article.findOne({ slug: slug }).lean();
    if (!article) throw new NotFoundError("Article");

    const articleJSON = await articleResponse(article, loggedUser);

    res.json({ article: articleJSON });
  } catch (error) {
    next(error);
  }
};

//* Update Article
const updateArticle = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { slug } = req.params;
    const article = await Article.findOne({ slug: slug }).lean();
    if (!article) throw new NotFoundError("Article");

    if (loggedUser._id !== article.authorId) {
      throw new ForbiddenError("article");
    }

    const { title, description, body } = req.body.article;
    const updateFields = {};
    if (title) {
      updateFields.slug = slugify(title);
      updateFields.title = title;
    }
    if (description) updateFields.description = description;
    if (body) updateFields.body = body;

    if (Object.keys(updateFields).length > 0) {
      await Article.updateOne({ _id: article._id }, { $set: updateFields });
    }

    const updatedArticle = await Article.findOne({ _id: article._id }).lean();

    const response = await articleResponse(updatedArticle, loggedUser);

    res.json({ article: response });
  } catch (error) {
    next(error);
  }
};

//* Delete Article
const deleteArticle = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { slug } = req.params;
    const article = await Article.findOne({ slug: slug }).lean();
    if (!article) throw new NotFoundError("Article");

    if (loggedUser._id !== article.authorId) {
      throw new ForbiddenError("article");
    }

    const s = await mongoose.startSession();
    await s.withTransaction(async () => {
      await Article.deleteOne({ _id: article._id }, { session: s });
      await Comment.deleteMany({ articleId: article._id }, { session: s });
      await Favorite.deleteMany({ "_id.articleId": article._id }, { session: s });
    });
    await s.endSession();

    res.json({ message: { body: ["Article deleted successfully"] } });
  } catch (error) {
    next(error);
  }
};

module.exports = {
  allArticles,
  createArticle,
  singleArticle,
  updateArticle,
  deleteArticle,
  articlesFeed,
};
