const { Post, User } = require("../models");
const { decorate } = require("../helper");
const opts = { include: [{ model: User, as: "author" }], order: [["createdAt", "DESC"]] };
const list = async (req, res) => {
  const { rows } = await Post.findAndCountAll(opts);
  for (const p of rows) { await p.getAuthor(); decorate(p); }
  res.json(rows);
};
const follow = async (req, res) => { const u = await User.findByPk(1); await u.addFollower(req.user); res.json({}); };
module.exports = { list, follow };
