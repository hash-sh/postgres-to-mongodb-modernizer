const { UnauthorizedError, NotFoundError } = require("../helper/customErrors");
const { articleResponse } = require("../helper/helpers");
const { Article, Favorite, mongoose } = require("../models");

//*  Favorite/Unfavorite Article
const favoriteToggler = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { slug } = req.params;

    const article = await Article.findOne({ slug: slug }).lean();
    if (!article) throw new NotFoundError("Article");

    const s = await mongoose.startSession();
    await s.withTransaction(async () => {
      if (req.method === "POST") {
        const key = { articleId: article._id, userId: loggedUser._id };
        const result = await Favorite.updateOne(
          { _id: key },
          { $setOnInsert: { _id: key } },
          { upsert: true, session: s }
        );
        if (result.upsertedCount === 1) {
          await Article.updateOne(
            { _id: article._id },
            { $inc: { favoritesCount: 1 } },
            { session: s, timestamps: false }
          );
        }
      } else if (req.method === "DELETE") {
        const key = { articleId: article._id, userId: loggedUser._id };
        const result = await Favorite.deleteOne({ _id: key }, { session: s });
        if (result.deletedCount === 1) {
          await Article.updateOne(
            { _id: article._id },
            { $inc: { favoritesCount: -1 } },
            { session: s, timestamps: false }
          );
        }
      }
    });
    await s.endSession();

    const freshArticle = await Article.findById(article._id).lean();
    const response = await articleResponse(freshArticle, loggedUser);
    res.json({ article: response });
  } catch (error) {
    next(error);
  }
};

module.exports = { favoriteToggler };
