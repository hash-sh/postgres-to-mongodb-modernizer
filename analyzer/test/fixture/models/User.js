const { Model } = require("sequelize");
module.exports = (sequelize, DataTypes) => {
  class User extends Model {
    static associate({ User, Post }) {
      this.hasMany(Post, { foreignKey: "userId" });
      this.belongsToMany(User, { through: "Follows", as: "followers", foreignKey: "userId" });
    }
    toJSON() { return { ...this.get(), password: undefined }; }
  }
  User.init({ email: { type: DataTypes.STRING, allowNull: false, unique: true }, password: DataTypes.STRING }, { sequelize, modelName: "User" });
  return User;
};
