const { UnauthorizedError, NotFoundError } = require("../helper/customErrors");
const { appendFollowers } = require("../helper/helpers");
const { User, Follower, mongoose } = require("../models");

// Helper to shape profile response
const shapeProfile = (doc) => ({
  username: doc.username,
  bio: doc.bio ?? null,
  image: doc.image ?? null,
  followersCount: doc.followersCount ?? 0,
  following: !!doc.following,
});

//? Profile
const getProfile = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    const { username } = req.params;

    const profile = await User.findOne({ username })
      .select("-email -password")
      .lean();

    if (!profile) throw new NotFoundError("User profile");

    await appendFollowers(loggedUser, profile);

    res.json({ profile: shapeProfile(profile) });
  } catch (error) {
    next(error);
  }
};

//* Follow/Unfollow Profile
const followToggler = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const { username } = req.params;

    const profile = await User.findOne({ username })
      .select("-email -password")
      .lean();

    if (!profile) throw new NotFoundError("User profile");

    const session = await mongoose.startSession();
    try {
      await session.withTransaction(async () => {
        const key = { userId: profile._id, followerId: loggedUser._id };
        if (req.method === "POST") {
          const result = await Follower.updateOne(
            { _id: key },
            { $setOnInsert: { _id: key } },
            { upsert: true, session }
          );
          if (result.upsertedCount === 1) {
            await User.updateOne(
              { _id: profile._id },
              { $inc: { followersCount: 1 } },
              { session }
            );
          }
        } else if (req.method === "DELETE") {
          const result = await Follower.deleteOne({ _id: key }, { session });
          if (result.deletedCount === 1) {
            await User.updateOne(
              { _id: profile._id },
              { $inc: { followersCount: -1 } },
              { session }
            );
          }
        }
      });
    } finally {
      await session.endSession();
    }

    const refreshed = await User.findById(profile._id)
      .select("-email -password")
      .lean();

    await appendFollowers(loggedUser, refreshed);

    res.json({ profile: shapeProfile(refreshed) });
  } catch (error) {
    next(error);
  }
};

module.exports = { getProfile, followToggler };
