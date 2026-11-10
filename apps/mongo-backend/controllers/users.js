const { User, mongoose, nextId } = require("../models");
const { jwtSign } = require("../helper/jwt");
const { bcryptHash, bcryptCompare } = require("../helper/bcrypt");
const {
  ValidationError,
  FieldRequiredError,
  AlreadyTakenError,
  NotFoundError,
} = require("../helper/customErrors");

// Register
const signUp = async (req, res, next) => {
  try {
    const { username, email, bio, image, password } = req.body.user;
    if (!username) throw new FieldRequiredError(`A username`);
    if (!email) throw new FieldRequiredError(`An email`);
    if (!password) throw new FieldRequiredError(`A password`);

    const userExists = await User.findOne({ email }).lean();
    if (userExists) throw new AlreadyTakenError("Email", "try logging in");

    const _id = await nextId("users");
    const hashed = await bcryptHash(password);

    const created = await User.create({
      _id,
      email,
      username,
      bio,
      image,
      password: hashed,
    });

    const token = await jwtSign(created);
    const userObj = {
      email: created.email,
      username: created.username,
      bio: created.bio,
      image: created.image,
      token,
    };

    res.status(201).json({ user: userObj });
  } catch (error) {
    next(error);
  }
};

// Login
const signIn = async (req, res, next) => {
  try {
    const { user } = req.body;

    const existent = await User.findOne({ email: user.email }).lean();
    if (!existent) throw new NotFoundError("Email", "sign in first");

    const pwdMatch = await bcryptCompare(user.password, existent.password);
    if (!pwdMatch) throw new ValidationError("Wrong email/password combination");

    const token = await jwtSign(existent);
    const userObj = {
      email: existent.email,
      username: existent.username,
      bio: existent.bio,
      image: existent.image,
      token,
    };

    res.json({ user: userObj });
  } catch (error) {
    next(error);
  }
};

module.exports = { signUp, signIn };
