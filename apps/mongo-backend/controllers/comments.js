const {
  NotFoundError,
  UnauthorizedError,
  FieldRequiredError,
  ForbiddenError,
} = require("../helper/customErrors");
const { appendFollowers } = require("../helper/helpers");
const { Article, Comment, User, nextId } = require("../models");

//? All Comments for Article
const allComments = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    const { slug } = req.params;

    const article = await Article.findOne({ slug: slug }).lean();
    if (!article) throw new NotFoundError("Article");

    const comments = await Comment.find({ articleId: article._id }).sort({ _id: 1 }).lean();

    const result = [];
    for (const comment of comments) {
      const author = await User.findById(comment.authorId).lean();
      const internal = {
        _id: author._id,
        username: author.username,
        bio: author.bio,
        image: author.image,
      };
      await appendFollowers(loggedUser, internal);
      result.push({
        id: comment._id,
        body: comment.body,
        createdAt: comment.createdAt,
        updatedAt: comment.updatedAt,
        author: {
          username: internal.username,
          bio: internal.bio,
          image: internal.image,
          following: internal.following,
          followersCount: internal.followersCount,
        },
      });
    }

    res.json({ comments: result });
  } catch (error) {
    next(error);
  }
};

//* Create Comment for Article
const createComment = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { body } = req.body.comment;
    if (!body) throw new FieldRequiredError("Comment body");

    const { slug } = req.params;
    const article = await Article.findOne({ slug: slug }).lean();
    if (!article) throw new NotFoundError("Article");

    const commentId = await nextId("comments");
    const comment = await Comment.create({
      _id: commentId,
      body: body,
      articleId: article._id,
      authorId: loggedUser._id,
    });

    const internal = {
      _id: loggedUser._id,
      username: loggedUser.username,
      bio: loggedUser.bio,
      image: loggedUser.image,
    };
    await appendFollowers(loggedUser, internal);

    res.status(201).json({
      comment: {
        id: comment._id,
        body: comment.body,
        createdAt: comment.createdAt,
        updatedAt: comment.updatedAt,
        author: {
          username: internal.username,
          bio: internal.bio,
          image: internal.image,
          following: internal.following,
          followersCount: internal.followersCount,
        },
      },
    });
  } catch (error) {
    next(error);
  }
};

//* Delete Comment for Article
const deleteComment = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { slug, commentId } = req.params;

    const comment = await Comment.findById(commentId).lean();
    if (!comment) throw new NotFoundError("Comment");

    if (loggedUser._id !== comment.authorId) {
      throw new ForbiddenError("comment");
    }

    await Comment.deleteOne({ _id: comment._id });

    res.json({ message: { body: ["Comment deleted successfully"] } });
  } catch (error) {
    next(error);
  }
};

module.exports = { allComments, createComment, deleteComment };
