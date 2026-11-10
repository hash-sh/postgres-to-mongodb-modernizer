const {
  AlreadyTakenError,
  FieldRequiredError,
  ForbiddenError,
  NotFoundError,
  UnauthorizedError,
} = require("../helper/customErrors");
const {
  appendFollowers,
  appendFavorites,
  appendTagList,
  slugify,
} = require("../helper/helpers");
const {
  User,
  Article,
  Tag,
  Favorite,
  Follower,
  mongoose,
  nextId,
} = require("../models");

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

    let articlesRows = [];
    let articlesCount = 0;

    if (favorited) {
      const user = await User.findOne({ username: favorited }).lean();
      if (!user) {
        // mimic original behavior where user.getFavorites would throw
        throw new NotFoundError("User not found");
      }

      const favFilter = { "_id.userId": user._id };
      const favoriteDocs = await Favorite.find(favFilter)
        .skip(skip)
        .limit(lim)
        .lean();

      const articleIds = favoriteDocs.map((f) => f._id.articleId);
      articlesRows = await Article.find({ _id: { $in: articleIds } })
        .populate({ path: "tagList", model: Tag, select: "name -_id" })
        .populate({ path: "author", model: User, select: "-email -password -followersCount -createdAt -updatedAt" })
        .sort({ createdAt: -1 })
        .lean();

      articlesCount = await Favorite.countDocuments(favFilter);
    } else {
      const articleFilter = {};
      if (author) {
        const authorDoc = await User.findOne({ username: author }).lean();
        if (authorDoc) articleFilter.authorId = authorDoc._id;
      }
      if (tag) {
        // inner join effect: only articles that have this tag
        const tagDoc = await Tag.findById(tag).lean();
        if (tagDoc) {
          articleFilter.tagList = tag;
        } else {
          // no matching tag, result empty
          articleFilter.tagList = null;
        }
      }

      const query = Article.find(articleFilter)
        .populate({ path: "tagList", model: Tag, select: "name -_id" })
        .populate({ path: "author", model: User, select: "-email -password -followersCount -createdAt -updatedAt" })
        .sort({ createdAt: -1 })
        .skip(skip)
        .limit(lim)
        .lean();

      const [rows, count] = await Promise.all([
        query.exec(),
        // emulate Sequelize count with joins (duplicate count per tag)
        Article.countDocuments(articleFilter),
      ]);

      articlesRows = rows;
      articlesCount = count;
    }

    for (let article of articlesRows) {
      const articleTags = article.tagList || [];
      appendTagList(articleTags, article);
      await appendFollowers(loggedUser, article);
      await appendFavorites(loggedUser, article);
    }

    res.json({ articles: articlesRows, articlesCount });
  } catch (error) {
    next(error);
  }
};
