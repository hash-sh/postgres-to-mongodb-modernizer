#!/usr/bin/env node
/**
 * Split a CommonJS module into top-level segments (deterministic, Babel AST).
 * Used by Stage 4 to convert/repair large files one function at a time, so every LLM request
 * fits the free tier's 8k tokens/minute.
 *
 * Output: JSON [{ name, kind: "function" | "exports" | "other", start, end, text }]
 * Usage: node split.js <file>
 */
const fs = require("fs");
const parser = require("@babel/parser");

function split(code) {
  const ast = parser.parse(code, { sourceType: "unambiguous" });
  const out = [];
  for (const node of ast.program.body) {
    // include leading comments with the statement they document
    const start = node.leadingComments && node.leadingComments.length ? node.leadingComments[0].start : node.start;
    let kind = "other";
    let name = null;
    if (node.type === "VariableDeclaration" && node.declarations.length === 1) {
      const d = node.declarations[0];
      if (d.init && ["ArrowFunctionExpression", "FunctionExpression"].includes(d.init.type)) {
        kind = "function";
        name = d.id.name;
      }
    } else if (node.type === "FunctionDeclaration") {
      kind = "function";
      name = node.id.name;
    } else if (node.type === "ExpressionStatement" && node.expression.type === "AssignmentExpression" &&
               code.slice(node.expression.left.start, node.expression.left.end).startsWith("module.exports")) {
      kind = "exports";
    }
    out.push({ name, kind, start, end: node.end, text: code.slice(start, node.end) });
  }
  return out;
}

if (require.main === module) {
  process.stdout.write(JSON.stringify(split(fs.readFileSync(process.argv[2], "utf8"))));
}
module.exports = { split };
