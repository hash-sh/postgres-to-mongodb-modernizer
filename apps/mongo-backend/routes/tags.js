const express = require("express");
const router = express.Router();
const { Tag } = require("../models");

// All Tags
router.get("/", async (req, res, next) => {
  try {
    const tagList = await Tag.find().lean();

    const tags = tagList.map((tag) => tag._id);

    res.json({ tags });
  } catch (error) {
    next(error);
  }
});

module.exports = router;
