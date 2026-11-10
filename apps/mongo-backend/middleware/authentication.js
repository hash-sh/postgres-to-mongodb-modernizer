const { NotFoundError } = require("../helper/customErrors");
const { jwtVerify } = require("../helper/jwt");
const { User } = require("../models");

const verifyToken = async (req, res, next) => {
  try {
    const { headers } = req;
    if (!headers.authorization) return next();

    const token = headers.authorization.split(" ")[1];
    if (!token) throw new SyntaxError("Token missing or malformed");

    const userVerified = await jwtVerify(token);
    if (!userVerified) throw new Error("Invalid Token");

    const user = await User.findOne({ email: userVerified.email }).lean();

    if (!user) {
      return next(new NotFoundError("User"));
    }

    const { email, ...rest } = user;
    req.loggedUser = rest;
    headers.email = userVerified.email;
    req.loggedUser.token = token;

    next();
  } catch (error) {
    next(error);
  }
};

module.exports = verifyToken;
