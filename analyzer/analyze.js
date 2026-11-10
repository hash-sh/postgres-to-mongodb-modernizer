#!/usr/bin/env node
/**
 * Deterministic static analyzer for a Sequelize + Express backend.
 *
 * Why a parser and not an LLM: every fact here (a field, an association, a call
 * site and its line number) must be exactly right and reproducible. @babel/parser
 * gives a real AST for modern JS; @babel/traverse gives scope-aware walking.
 *
 * Output (stdout, JSON):
 *   models       - fields, types, constraints, toJSON-hidden fields, associations
 *   associations - flattened, with cardinality (1:1, 1:N, N:M, self-referential)
 *   routes       - METHOD + full path -> handler (file + function)
 *   callSites    - every Sequelize query / association-mixin call, with options
 *   functions    - per function: call sites + helper functions it calls (call graph)
 *   semanticNotes- ORM behaviours that a rewrite must preserve (pattern rules)
 *
 * Usage: node analyze.js <backendDir>
 */
const fs = require("fs");
const path = require("path");
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;

const QUERY_METHODS = new Set([
  "findAll", "findOne", "findByPk", "findAndCountAll", "findOrCreate", "create", "bulkCreate",
  "update", "destroy", "count", "save", "reload", "increment", "decrement", "upsert", "query", "transaction", "sync",
]);
const READ = new Set(["findAll", "findOne", "findByPk", "findAndCountAll", "count", "reload", "get", "has"]);

function parseFile(file) {
  const code = fs.readFileSync(file, "utf8");
  return parser.parse(code, { sourceType: "unambiguous", plugins: ["jsx"], errorRecovery: false });
}

function listJs(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === "node_modules" || e.name.startsWith(".")) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...listJs(p));
    else if (e.name.endsWith(".js") && !e.name.endsWith(".test.js")) out.push(p);
  }
  return out.sort();
}

/** Convert a literal-ish AST node into a JSON value (best effort, deterministic). */
function literal(node, scope = null, seen = new Set()) {
  if (!node) return null;
  // Resolve `const searchOptions = {...}` / `includeOptions` so options passed by variable are visible.
  if (node.type === "Identifier" && scope && !seen.has(node.name)) {
    const b = scope.getBinding(node.name);
    const init = b && b.path.isVariableDeclarator() && b.path.node.init;
    if (init && (init.type === "ObjectExpression" || init.type === "ArrayExpression")) {
      return literal(init, b.path.scope, new Set([...seen, node.name]));
    }
  }
  const rec = (n) => literal(n, scope, seen);
  switch (node.type) {
    case "StringLiteral": case "NumericLiteral": case "BooleanLiteral": return node.value;
    case "NullLiteral": return null;
    case "Identifier": return { $id: node.name };
    case "MemberExpression": return { $id: memberName(node) };
    case "ArrayExpression": return node.elements.map(rec);
    case "ObjectExpression": {
      const o = {};
      for (const p of node.properties) {
        if (p.type === "SpreadElement") {
          // e.g. ...(tag && { where: { name: tag } })  -> conditional option
          const arg = p.argument.type === "LogicalExpression" ? p.argument.right : p.argument;
          const inner = rec(arg);
          if (inner && typeof inner === "object" && !Array.isArray(inner)) {
            for (const [k, v] of Object.entries(inner)) o[k] = { $conditional: v };
          }
          continue;
        }
        const key = p.key.type === "Identifier" ? p.key.name : p.key.value;
        o[key] = p.type === "ObjectMethod" ? { $fn: true } : rec(p.value);
      }
      return o;
    }
    case "TemplateLiteral": return { $template: true };
    case "BinaryExpression": return { $expr: `${literalStr(node.left)} ${node.operator} ${literalStr(node.right)}` };
    case "CallExpression": return { $call: memberName(node.callee) };
    case "ArrowFunctionExpression": case "FunctionExpression": return { $fn: true };
    default: return { $expr: node.type };
  }
}
const literalStr = (n) => (n.type === "Identifier" ? n.name : n.type === "NumericLiteral" ? String(n.value) : n.type);

function memberName(node) {
  if (!node) return "";
  if (node.type === "Identifier") return node.name;
  if (node.type === "ThisExpression") return "this";
  if (node.type === "MemberExpression" || node.type === "OptionalMemberExpression") {
    const prop = node.computed ? `[${literalStr(node.property)}]` : node.property.name;
    return `${memberName(node.object)}.${prop}`;
  }
  if (node.type === "CallExpression") return `${memberName(node.callee)}()`;
  return node.type;
}

// ---------------------------------------------------------------- models
function analyzeModel(file) {
  const ast = parseFile(file);
  const model = { file, name: null, fields: {}, options: {}, associations: [], toJSONHidden: [] };
  traverse(ast, {
    ClassDeclaration(p) { model.name = p.node.id.name; },
    CallExpression(p) {
      const callee = p.node.callee;
      if (callee.type !== "MemberExpression") return;
      const m = callee.property.name;
      if (m === "init" && callee.object.type === "Identifier") {
        model.name = model.name || callee.object.name;
        model.fields = normalizeFields(literal(p.node.arguments[0]));
        const opts = literal(p.node.arguments[1]) || {};
        delete opts.sequelize;
        model.options = opts;
      }
      if (["belongsTo", "hasMany", "hasOne", "belongsToMany"].includes(m) && callee.object.type === "ThisExpression") {
        const target = p.node.arguments[0].name;
        const opts = literal(p.node.arguments[1]) || {};
        model.associations.push({ type: m, target, ...opts, line: p.node.loc.start.line });
      }
    },
    ClassMethod(p) {
      if (p.node.key.name !== "toJSON") return;
      p.traverse({
        ObjectProperty(q) {
          if (q.node.value.type === "Identifier" && q.node.value.name === "undefined") model.toJSONHidden.push(q.node.key.name);
        },
      });
    },
  });
  return model;
}

function normalizeFields(raw) {
  const out = {};
  for (const [k, v] of Object.entries(raw || {})) {
    if (v && v.$id) out[k] = { type: v.$id.replace(/^DataTypes\./, "") };
    else {
      const f = { ...v };
      if (f.type && f.type.$id) f.type = f.type.$id.replace(/^(DataTypes|Sequelize)\./, "");
      out[k] = f;
    }
  }
  return out;
}

/** Sequelize association mixin names, e.g. as:"tagList" on belongsToMany -> getTagList/addTagList/... */
function mixinsFor(owner, a) {
  const cap = (s) => s[0].toUpperCase() + s.slice(1);
  const plural = (s) => (s.endsWith("s") ? s : s + "s");
  const singular = (s) => (s.endsWith("s") ? s.slice(0, -1) : s);
  const name = a.as || (a.type === "belongsTo" || a.type === "hasOne" ? a.target : plural(a.target));
  const many = a.type === "hasMany" || a.type === "belongsToMany";
  const P = cap(many ? plural(name) : name), S = cap(many ? singular(name) : name);
  const ops = many
    ? { [`get${P}`]: "read", [`count${P}`]: "read", [`has${S}`]: "read", [`has${P}`]: "read",
        [`add${S}`]: "write", [`add${P}`]: "write", [`remove${S}`]: "write", [`remove${P}`]: "write",
        [`set${P}`]: "write", [`create${S}`]: "write" }
    : { [`get${P}`]: "read", [`set${P}`]: "write", [`create${P}`]: "write" };
  // Sequelize keeps the user-given alias casing for plural aliases like "tagList" (getTagList, addTagList)
  if (a.as && many && !a.as.endsWith("s")) {
    const A = cap(a.as);
    Object.assign(ops, { [`get${A}`]: "read", [`count${A}`]: "read", [`has${A}`]: "read", [`add${A}`]: "write", [`remove${A}`]: "write", [`set${A}`]: "write" });
  }
  return Object.entries(ops).map(([method, kind]) => ({ method, kind, owner, association: a }));
}

function cardinality(owner, a, allAssocs) {
  if (a.target === owner) return "N:M self-referential";
  if (a.type === "belongsToMany") return "N:M";
  if (a.type === "hasMany") return "1:N";
  if (a.type === "hasOne") return "1:1";
  // belongsTo: inverse side of a hasMany/hasOne
  const inverse = allAssocs.find((x) => x.owner === a.target && x.target === owner && x.type !== "belongsTo");
  return inverse && inverse.type === "hasOne" ? "1:1" : "N:1";
}

// ---------------------------------------------------------------- routes
function analyzeRoutes(backend) {
  const routes = [];
  const index = path.join(backend, "index.js");
  const mounts = []; // [prefix, routerFile]
  const requires = (ast, file) => {
    const map = {};
    traverse(ast, {
      VariableDeclarator(p) {
        const init = p.node.init;
        if (init && init.type === "CallExpression" && init.callee.name === "require" && init.arguments[0].type === "StringLiteral") {
          const spec = init.arguments[0].value;
          if (!spec.startsWith(".")) return;
          let target = path.resolve(path.dirname(file), spec);
          if (!target.endsWith(".js")) target += fs.existsSync(target + ".js") ? ".js" : "/index.js";
          if (p.node.id.type === "Identifier") map[p.node.id.name] = { file: target, export: null };
          else for (const prop of p.node.id.properties) map[prop.value.name] = { file: target, export: prop.key.name };
        }
      },
    });
    return map;
  };
  const walkRouter = (file, prefix) => {
    const ast = parseFile(file);
    const req = requires(ast, file);
    traverse(ast, {
      CallExpression(p) {
        const c = p.node.callee;
        if (c.type !== "MemberExpression" || !["router", "app"].includes(c.object.name)) return;
        const verb = c.property.name;
        const args = p.node.arguments;
        if (verb === "use" && args.length >= 2 && args[0].type === "StringLiteral") {
          const r = args[args.length - 1];
          if (r.type === "Identifier" && req[r.name]) mounts.push([join(prefix, args[0].value), req[r.name].file]);
          return;
        }
        if (!["get", "post", "put", "delete", "patch"].includes(verb) || args[0].type !== "StringLiteral") return;
        const handlers = args.slice(1);
        const last = handlers[handlers.length - 1];
        const middleware = handlers.slice(0, -1).map((h) => h.name);
        let handler;
        if (last.type === "Identifier" && req[last.name]) handler = { file: req[last.name].file, function: req[last.name].export || last.name };
        else handler = { file, function: `<inline ${verb.toUpperCase()} ${args[0].value}>`, line: last.loc.start.line };
        routes.push({ method: verb.toUpperCase(), path: join(prefix, args[0].value), middleware, handler, routeFile: file });
      },
    });
  };
  walkRouter(index, "");
  for (let i = 0; i < mounts.length; i++) walkRouter(mounts[i][1], mounts[i][0]);
  return routes;
}
const join = (a, b) => (a + "/" + b).replace(/\/+/g, "/").replace(/(.)\/$/, "$1");

// ---------------------------------------------------------------- call sites
function analyzeCalls(files, models, mixinIndex) {
  const modelNames = new Set(models.map((m) => m.name));
  const callSites = [];
  const functions = {}; // "file#fn" -> { callSites:[], calls:[] }
  for (const file of files) {
    const ast = parseFile(file);
    const imported = {};
    traverse(ast, {
      VariableDeclarator(p) {
        const init = p.node.init;
        if (init && init.type === "CallExpression" && init.callee.name === "require" && p.node.id.type === "ObjectPattern")
          for (const prop of p.node.id.properties) imported[prop.value.name] = init.arguments[0].value;
      },
    });
    traverse(ast, {
      CallExpression(p) {
        const fnPath = p.getFunctionParent();
        const fnName = enclosingName(fnPath, file);
        const key = `${file}#${fnName}`;
        functions[key] = functions[key] || { file, function: fnName, callSites: [], calls: [] };
        const c = p.node.callee;
        if (c.type === "Identifier" && imported[c.name] && !modelNames.has(c.name)) {
          if (!functions[key].calls.includes(c.name)) functions[key].calls.push(c.name);
          return;
        }
        if (c.type !== "MemberExpression" && c.type !== "OptionalMemberExpression") return;
        const method = c.property.name;
        const receiver = memberName(c.object);
        let site = null;
        if (QUERY_METHODS.has(method) && (modelNames.has(receiver) || method === "save" || method === "destroy" || method === "query" || method === "sync" || method === "transaction")) {
          const opts = literal(p.node.arguments[0], p.scope);
          site = { kind: "query", method, receiver, model: modelNames.has(receiver) ? receiver : null, options: opts };
          if (method === "query") site.rawSQL = true;
          if (method === "transaction") site.transaction = true;
        } else if (mixinIndex[method]) {
          site = { kind: "association", method, receiver, candidates: mixinIndex[method].map((m) => `${m.owner}.${m.association.as || m.association.target}`), options: literal(p.node.arguments[0], p.scope) };
          site.op = mixinIndex[method][0].kind;
        }
        if (!site) return;
        site.file = file;
        site.line = p.node.loc.start.line;
        site.function = fnName;
        site.op = site.op || (READ.has(method) ? "read" : method === "sync" ? "ddl" : "write");
        site.features = features(site.options);
        callSites.push(site);
        functions[key].callSites.push(callSites.length - 1);
      },
    });
  }
  return { callSites, functions };
}

function enclosingName(fnPath, file) {
  let p = fnPath;
  while (p) {
    const n = p.node;
    if (n.id && n.id.name) return n.id.name;
    if (p.parent.type === "VariableDeclarator") return p.parent.id.name;
    if (p.parent.type === "ObjectProperty" || p.parent.type === "ClassMethod") return p.parent.key.name;
    if (n.type === "ClassMethod" || n.type === "ObjectMethod") return n.key.name;
    if (p.parent.type === "CallExpression") {
      // express inline handler or IIFE
      const callee = memberName(p.parent.callee);
      const first = p.parent.arguments[0];
      return `<${callee}${first && first.type === "StringLiteral" ? " " + first.value : ""}>`;
    }
    p = p.getFunctionParent ? p.parentPath && p.parentPath.getFunctionParent() : null;
  }
  return "<module>";
}

function features(opts) {
  const f = [];
  if (!opts || typeof opts !== "object") return f;
  const walk = (o) => {
    if (!o || typeof o !== "object") return;
    for (const [k, v] of Object.entries(o)) {
      if (["include", "where", "order", "limit", "offset", "attributes", "transaction", "distinct", "group", "raw"].includes(k) && !f.includes(k)) f.push(k);
      if (v && v.$conditional && !f.includes(`conditional:${k}`)) f.push(`conditional:${k}`);
      walk(v && v.$conditional ? v.$conditional : v);
    }
  };
  walk(opts);
  return f.sort();
}

// ---------------------------------------------------------------- semantic notes (pattern rules)
function semanticNotes(callSites, models) {
  const notes = [];
  const n2n = new Set();
  for (const m of models) for (const a of m.associations) if (a.type === "belongsToMany" || a.type === "hasMany") n2n.add(a.as || a.target);
  for (const s of callSites) {
    const o = s.options || {};
    const inc = (o.include && o.include.$id) ? [o.include.$id] : Array.isArray(o.include) ? o.include : [];
    if (s.method === "findAndCountAll" && !o.distinct && inc.length) {
      notes.push({ rule: "COUNT_WITH_INCLUDE_NOT_DISTINCT", file: s.file, line: s.line, function: s.function,
        text: "findAndCountAll with `include` and no `distinct: true`: Sequelize's count counts JOINED ROWS. With a to-many include (tagList) an article with k>1 tags is counted k times (LEFT JOIN => an article with 0 tags counts 1). A filtered include (`where` on the include) becomes an INNER JOIN. The rewrite must reproduce this count." });
    }
    if (s.options && JSON.stringify(s.options).includes("offset * limit")) {
      notes.push({ rule: "OFFSET_IS_PAGE_INDEX", file: s.file, line: s.line, function: s.function,
        text: "`offset: offset * limit` - the query parameter `offset` is a PAGE index, not a row offset. skip = offset*limit." });
    }
    if (s.method === "getFavorites" || s.method === "getFollowing") {
      notes.push({ rule: "MIXIN_WITH_OPTIONS", file: s.file, line: s.line, function: s.function,
        text: `${s.method}(...) runs a JOIN through the junction table; the receiver may be null if the preceding findOne found nothing (=> TypeError => HTTP 500 in the original).` });
    }
    if (s.method === "findAll" && !(s.options && s.options.order)) {
      notes.push({ rule: "UNORDERED_FINDALL", file: s.file, line: s.line, function: s.function,
        text: "findAll without `order`: result order is unspecified by SQL. Treat as an unordered set when comparing." });
    }
  }
  return notes;
}

// ---------------------------------------------------------------- main
function main() {
  const backend = path.resolve(process.argv[2] || ".");
  const rel = (p) => path.relative(backend, p);
  const modelFiles = listJs(path.join(backend, "models")).filter((f) => path.basename(f) !== "index.js");
  const models = modelFiles.map(analyzeModel);

  const associations = [];
  for (const m of models) for (const a of m.associations) associations.push({ owner: m.name, ...a });
  for (const a of associations) a.cardinality = cardinality(a.owner, a, associations);

  const mixinIndex = {};
  for (const a of associations) for (const mx of mixinsFor(a.owner, a)) (mixinIndex[mx.method] = mixinIndex[mx.method] || []).push(mx);

  const codeFiles = listJs(backend).filter((f) => !f.includes(`${path.sep}migrations${path.sep}`) && !f.includes(`${path.sep}seeders${path.sep}`) && !modelFiles.includes(f));
  const { callSites, functions } = analyzeCalls(codeFiles, models, mixinIndex);
  const routes = analyzeRoutes(backend);

  // relativize paths for stable output
  for (const m of models) m.file = rel(m.file);
  for (const s of callSites) s.file = rel(s.file);
  for (const r of routes) { r.handler.file = rel(r.handler.file); r.routeFile = rel(r.routeFile); }
  const fns = {};
  for (const v of Object.values(functions)) if (v.callSites.length || v.calls.length) fns[`${rel(v.file)}#${v.function}`] = { ...v, file: rel(v.file) };

  const out = {
    generatedBy: "analyzer/analyze.js (@babel/parser)",
    backend: path.basename(path.dirname(backend)) + "/" + path.basename(backend),
    models, associations, routes, callSites, functions: fns,
    semanticNotes: semanticNotes(callSites, models).map((n) => ({ ...n, file: rel(path.resolve(backend, n.file)) })),
  };
  process.stdout.write(JSON.stringify(out, null, 2) + "\n");
}

if (require.main === module) main();
module.exports = { analyzeModel, mixinsFor, cardinality, literal, features, semanticNotes, analyzeRoutes, analyzeCalls, parseFile };
