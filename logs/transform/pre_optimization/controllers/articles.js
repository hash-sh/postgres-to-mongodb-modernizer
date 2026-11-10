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
    for (const doc of articlesDocs) {
      // tagList is already an array of strings; ensure it's present
      if (!doc.tagList) doc.tagList = [];

      // build full article response using helper (adds author, favorited, counts, etc.)
      const resp = await articleResponse(doc, loggedUser);
      articles.push(resp);
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
      authorId: loggedUser._id,
      tagList: [],
      favoritesCount: 0
    });

    for (const tag of tagList) {
      const trimmedTag = tag.trim();
      const tagInDB = await Tag.findById(trimmedTag).lean();

      if (tagInDB) {
        await Article.updateOne({ _id: article._id }, { $addToSet: { tagList: trimmedTag } });
      } else if (trimmedTag.length > 2) {
        await Tag.create({ _id: trimmedTag });
        await Article.updateOne({ _id: article._id }, { $addToSet: { tagList: trimmedTag } });
      }
    }

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

    const articlesCount = await Article.countDocuments({
      authorId: { $in: followedUserIds },
    });

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
    if (title) {
      article.slug = slugify(title);
      article.title = title;
    }
    if (description) article.description = description;
    if (body) article.body = body;

    await Article.updateOne(
      { _id: article._id },
      {
        $set: {
          slug: article.slug,
          title: article.title,
          description: article.description,
          body: article.body,
        },
      }
    );

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

    await Article.deleteOne({ _id: article._id });
    await Comment.deleteMany({ articleId: article._id });
    await Favorite.deleteMany({ "_id.articleId": article._id });

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
