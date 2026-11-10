const { Favorite, Follower, User } = require("../models");

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
    const authorId = toAppend.author._id;

    const following =
      userId &&
      (await Follower.findOne({
        "_id.userId": authorId,
        "_id.followerId": userId,
      }).lean());

    toAppend.author.following = loggedUser ? !!following : false;

    const followersCount = await Follower.countDocuments({
      "_id.userId": authorId,
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

const articleResponse = async (articleDoc, loggedUser) => {
  const article = articleDoc;
  const authorDoc = await User.findById(article.authorId).lean();

  let following = false;
  if (loggedUser && authorDoc) {
    const follow = await Follower.findOne({
      "_id.userId": authorDoc._id,
      "_id.followerId": loggedUser._id,
    }).lean();
    following = !!follow;
  }

  let favorited = false;
  if (loggedUser) {
    const fav = await Favorite.findOne({
      "_id.articleId": article._id,
      "_id.userId": loggedUser._id,
    }).lean();
    favorited = !!fav;
  }

  return {
    slug: article.slug,
    title: article.title,
    description: article.description,
    body: article.body,
    createdAt: article.createdAt,
    updatedAt: article.updatedAt,
    tagList: Array.isArray(article.tagList) ? [...article.tagList] : [],
    favorited,
    favoritesCount: article.favoritesCount ?? 0,
    author: {
      username: authorDoc ? authorDoc.username : null,
      bio: authorDoc ? authorDoc.bio : null,
      image: authorDoc ? authorDoc.image : null,
      following,
      followersCount: authorDoc ? (authorDoc.followersCount ?? 0) : 0,
    },
  };
};

module.exports = {
  slugify,
  appendTagList,
  appendFavorites,
  appendFollowers,
  articleResponse,
};
