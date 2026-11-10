const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const { execFileSync } = require("child_process");

const fixture = path.join(__dirname, "fixture");
const inv = JSON.parse(execFileSync("node", [path.join(__dirname, "..", "analyze.js"), fixture]).toString());

test("extracts models, fields, constraints and toJSON-hidden fields", () => {
  const user = inv.models.find((m) => m.name === "User");
  assert.deepStrictEqual(Object.keys(user.fields), ["email", "password"]);
  assert.strictEqual(user.fields.email.allowNull, false);
  assert.strictEqual(user.fields.email.unique, true);
  assert.deepStrictEqual(user.toJSONHidden, ["password"]);
});

test("classifies association cardinality, including self-reference", () => {
  const c = Object.fromEntries(inv.associations.map((a) => [`${a.owner}.${a.as || a.target}`, a.cardinality]));
  assert.strictEqual(c["User.Post"], "1:N");
  assert.strictEqual(c["User.followers"], "N:M self-referential");
  assert.strictEqual(c["Post.author"], "N:1");
});

test("resolves routes through app.use mounts", () => {
  const r = inv.routes.map((x) => `${x.method} ${x.path} ${x.handler.function}`).sort();
  assert.deepStrictEqual(r, ["GET /api/posts list", "POST /api/posts/:id/follow follow"]);
});

test("finds query + association-mixin call sites, resolves options passed by variable", () => {
  const fac = inv.callSites.find((s) => s.method === "findAndCountAll");
  assert.deepStrictEqual(fac.features, ["include", "order"]);
  assert.strictEqual(fac.function, "list");
  const mixin = inv.callSites.find((s) => s.method === "addFollower");
  assert.strictEqual(mixin.kind, "association");
  assert.deepStrictEqual(mixin.candidates, ["User.followers"]);
  assert.strictEqual(mixin.op, "write");
});

test("flags N+1 (query inside loop) and the count-with-include ORM semantic", () => {
  assert.strictEqual(inv.callSites.find((s) => s.method === "getAuthor").inLoop, true);
  assert.ok(inv.semanticNotes.some((n) => n.rule === "COUNT_WITH_INCLUDE_NOT_DISTINCT"));
  assert.deepStrictEqual(inv.functions["controllers/posts.js#list"].loopCalls, ["decorate"]);
});
