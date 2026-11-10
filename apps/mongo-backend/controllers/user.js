const { UnauthorizedError } = require("../helper/customErrors");
const { bcryptHash } = require("../helper/bcrypt");
const { User, mongoose } = require("../models");

// fields that are allowed to be sent to client for a user
const PUBLIC_USER_FIELDS = ["username", "bio", "image", "token", "email"];

// build a plain user object containing only public fields
const buildPublicUser = (src) => {
  const out = {};
  PUBLIC_USER_FIELDS.forEach((key) => {
    if (src[key] !== undefined) out[key] = src[key];
  });
  return out;
};

//* Current User
const currentUser = async (req, res, next) => {
  try {
    const { loggedUser } = req;
    if (!loggedUser) throw new UnauthorizedError();

    // email is stored in header by auth middleware
    const email = req.headers.email;
    delete req.headers.email;

    const userForResp = buildPublicUser({ ...loggedUser, email });
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

      Object.entries(user).forEach(([key, value]) => {
        if (value !== undefined && key !== "password") dbUser[key] = value;
      });

      // Preserve original buggy condition (always true)
      if (password !== undefined || password !== "") {
        dbUser.password = await bcryptHash(password);
      }

      await dbUser.save({ session });

      // update loggedUser reference for consistency (only public fields)
      Object.assign(loggedUser, dbUser.toObject());
    });
    await session.endSession();

    const userForResp = buildPublicUser(loggedUser);
    res.json({ user: userForResp });
  } catch (error) {
    next(error);
  }
};

module.exports = { currentUser, updateUser };
