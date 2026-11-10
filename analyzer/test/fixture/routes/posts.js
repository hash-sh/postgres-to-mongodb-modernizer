const express = require("express");
const router = express.Router();
const { list, follow } = require("../controllers/posts");
router.get("/", list);
router.post("/:id/follow", follow);
module.exports = router;
