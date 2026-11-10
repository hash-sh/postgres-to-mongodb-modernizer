const { Model } = require("sequelize");
module.exports = (sequelize, DataTypes) => {
  class Post extends Model {
    static associate({ User }) { this.belongsTo(User, { foreignKey: "userId", as: "author" }); }
  }
  Post.init({ title: DataTypes.STRING }, { sequelize, modelName: "Post" });
  return Post;
};
