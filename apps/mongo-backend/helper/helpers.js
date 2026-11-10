const { User, Favorite, Follower, Tag } = require("../models");

const slugify = (string) => {
  return string.trim().toLowerCase().replace(/\W|_/g, "-");
};

const appendTagList = (articleTags, article) => {
  const tagList = articleTags.map((tag) => tag._id);
  if (!article) return tagList;
  article.tagList = tagList;
};

const appendFavorites = async (loggedUser, article) => {
  const articleId = article._id;
  const userId = loggedUser ? loggedUser._id : null;

  const favorited =
    userId &&
    (await Favorite.findOne({
      "_id.articleId": articleId,
      "_id.userId": userId,
    }).lean());

  article.favorited = loggedUser ? !!favorited : false;

  const favoritesCount = await Favorite.countDocuments({
    "_id.articleId": articleId,
  });
  article.favoritesCount = favoritesCount;
};

const appendFollowers = async (loggedUser, toAppend) => {
  const userId = loggedUser ? loggedUser._id : null;

  if (toAppend?.author) {
    const author = await User.findById(toAppend.author._id).lean();

    const following =
      userId &&
      (await Follower.findOne({
        "_id.userId": author._id,
        "_id.followerId": userId,
      }).lean());

    toAppend.author.following = loggedUser ? !!following : false;

    const followersCount = await Follower.countDocuments({
      "_id.userId": author._id,
    });
    toAppend.author.followersCount = followersCount;
  } else {
    const following =
      userId &&
      (await Follower.findOne({
        "_id.userId": toAppend._id,
        "_id.followerId": userId,
      }).lean());

    toAppend.following = loggedUser ? !!following : false;

    const followersCount = await Follower.countDocuments({
      "_id.userId": toAppend._id,
    });
    toAppend.followersCount = followersCount;
  }
};

module.exports = { slugify, appendTagList, appendFavorites, appendFollowers };
