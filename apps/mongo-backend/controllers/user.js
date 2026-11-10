const { UnauthorizedError } = require("../helper/customErrors");
const { bcryptHash } = require("../helper/bcrypt");
const { User, mongoose } = require("../models");

// fields that are allowed to be sent to client for a user (GET /api/user)
const PUBLIC_USER_FIELDS = ["username", "bio", "image", "token", "email"];
// fields for the response of PUT /api/user (must NOT include email)
const UPDATE_PUBLIC_FIELDS = ["username", "bio", "image", "token"];

// build a plain user object containing only selected public fields
const buildPublicUser = (src, fields) => {
  const out = {};
  fields.forEach((key) => {
    if (src[key] !== undefined) out[key] = src[key];
  });
  return out;
};

//* Current User
const currentUser = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const email = req.headers.email;
    delete req.headers.email;

    const userForResp = buildPublicUser({ ...loggedUser, email }, PUBLIC_USER_FIELDS);
    res.json({ user: userForResp });
  } catch (error) {
    next(error);
  }
};

//* Update User
const updateUser = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    const {
      user: { password },
      user,
    } = req.body;

    const session = await mongoose.startSession();
    await session.withTransaction(async () => {
      const dbUser = await User.findOne({ _id: loggedUser._id }).session(session);
      if (!dbUser) throw new UnauthorizedError();

      // Apply updates, ignoring password and email (email updates are not allowed here)
      Object.entries(user).forEach(([key, value]) => {
        if (value !== undefined && key !== "password" && key !== "email") {
          dbUser[key] = value;
        }
      });

      // Preserve original buggy condition (always true)
      if (password !== undefined || password !== "") {
        dbUser.password = await bcryptHash(password);
      }

      await dbUser.save({ session });
    });
    await session.endSession();

    // Fetch fresh data for response (lean to avoid Mongoose magic)
    const freshUser = await User.findOne({ _id: loggedUser._id }).lean();
    const userForResp = buildPublicUser(
      { ...freshUser, token: loggedUser.token },
      UPDATE_PUBLIC_FIELDS
    );
    res.json({ user: userForResp });
  } catch (error) {
    next(error);
  }
};

module.exports = { currentUser, updateUser };
