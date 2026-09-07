/**
 * c121/c360: static HTTP method/path, query-key and required response/event-field
 * presence checks using Python AST and the TypeScript compiler. No server, DB or
 * backend dependency imports. This is not runtime JSON/value-type validation.
 * npm run verify:contract also executes representative real-source mutations.
 */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import ts from "typescript";
import { MUTATIONS } from "./contract-mutations.mjs";

const API_DIR = new URL("../src/api/", import.meta.url);
const APP_DIR = fileURLToPath(new URL("../", import.meta.url));
const mutationName = process.argv[2];
if (mutationName === "--self-test") {
  for (const [name, mutation] of Object.entries(MUTATIONS)) {
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url), name], { encoding: "utf8", timeout: 60_000 });
    const output = result.stdout + result.stderr;
    if (result.status !== 1 || !output.includes(mutation.expected)) {
      console.error(`FAIL mutation ${name}: expected contract rejection ${mutation.expected}\n${output}`);
      process.exit(1);
    }
    console.log(`PASS discriminating mutation: ${name}`);
  }
  console.log(`${Object.keys(MUTATIONS).length} contract mutations rejected`);
  process.exit(0);
}
if (mutationName && !MUTATIONS[mutationName]) throw new Error("Unknown contract mutation");
const mutation = MUTATIONS[mutationName];
const exported = spawnSync("python3", [fileURLToPath(new URL("backend-contracts.py", import.meta.url))], { encoding: "utf8" });
if (exported.status !== 0) throw new Error("Backend contract extraction failed: " + exported.stderr);
const inventory = JSON.parse(exported.stdout);
mutation?.inventory?.(inventory);
const configFile = ts.readConfigFile(APP_DIR + "tsconfig.json", ts.sys.readFile);
if (configFile.error) throw new Error("Cannot read TypeScript configuration");
const config = ts.parseJsonConfigFileContent(configFile.config, ts.sys, APP_DIR);
const host = ts.createCompilerHost(config.options);
const readSource = host.readFile.bind(host);
let mutated = false;
host.readFile = path => {
  const source = readSource(path);
  if (!mutation?.file || !path.endsWith(mutation.file) || source === undefined) return source;
  const changed = mutation.source(source);
  if (changed === source) throw new Error("Mutation anchor no longer matches: " + mutationName);
  mutated = true;
  return changed;
};
const program = ts.createProgram(config.fileNames, config.options, host);
if (mutation?.file && !mutated) throw new Error("Mutation source not loaded: " + mutationName);
const checker = program.getTypeChecker();
const calls = [];
const unresolved = [];
let responseChecks = 0;
let queryChecks = 0;
let eventChecks = 0;

function shape(path) {
  return path.split("/").filter(Boolean).map(part => /^\{[^}]+\}$/.test(part) ? "*" : part);
}
function equalShape(a, b) { return a.length === b.length && a.every((value, i) => value === b[i]); }
function property(node, key) {
  return node?.properties?.find(p => p.name && (ts.isIdentifier(p.name) || ts.isStringLiteral(p.name)) && p.name.text === key);
}
function literalPath(node) {
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isTemplateExpression(node)) {
    const path = node.head.text + node.templateSpans.map(span => "*" + span.literal.text).join("");
    return path.split("/").every(part => !part.includes("*") || part === "*") ? path : null;
  }
  return null;
}
function fieldsOfSchema(name, seen = new Set()) {
  if (seen.has(name)) throw new Error("Cyclic backend schema: " + name);
  const schema = inventory.schemas[name];
  if (!schema) return null;
  seen.add(name);
  return Object.assign({}, ...schema.bases.map(base => fieldsOfSchema(base, new Set(seen)) ?? {}), schema.fields);
}

for (const file of readdirSync(API_DIR).sort()) {
  if (!file.endsWith(".ts") || ["client.ts", "index.ts", "operation.ts"].includes(file)) continue;
  const source = program.getSourceFile(fileURLToPath(new URL(file, API_DIR)));
  if (!source) throw new Error("Client source not in TypeScript program: " + file);
  const imports = new Map();
  for (const statement of source.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || statement.moduleSpecifier.text !== "./client") continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const item of bindings.elements) {
      const original = (item.propertyName ?? item.name).text;
      if (["request", "requestText", "requestWithHeaders"].includes(original)) imports.set(item.name.text, original);
    }
  }
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && imports.has(node.expression.text)) {
      const where = file + ":" + (source.getLineAndCharacterOfPosition(node.getStart()).line + 1);
      const path = node.arguments[0] ? literalPath(node.arguments[0]) : null;
      const options = node.arguments[1];
      if (path === null || (options && !ts.isObjectLiteralExpression(options))) {
        unresolved.push(where + ": non-literal path or request options");
      } else {
        const methodNode = property(options, "method")?.initializer;
        const method = methodNode ? (ts.isStringLiteralLike(methodNode) ? methodNode.text : null) : "GET";
        // A spread can override the inferred method. These calls need an explicit
        // method after every spread; do not silently call an unknown operation GET.
        const spreads = options?.properties.filter(ts.isSpreadAssignment) ?? [];
        const methodProperty = property(options, "method");
        if (method === null || spreads.some(p => !methodProperty || p.pos > methodProperty.pos)) {
          unresolved.push(where + ": unresolved/overridable HTTP method");
        } else {
          calls.push({ path, method, where, node, options, wrapper: imports.get(node.expression.text) });
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}

let failures = 0;
function fail(message) { failures++; console.error("FAIL " + message); }
for (const call of calls) {
  const route = inventory.routes.find(r => r.method === call.method && equalShape(shape(r.path), shape(call.path)));
  if (!route) { fail(call.where + " " + call.method + " " + call.path + ": no backend route"); continue; }
  const query = property(call.options, "query");
  if (query) {
    const expression = ts.isShorthandPropertyAssignment(query) ? query.name : query.initializer;
    if (!expression) { fail(call.where + ": unresolved query expression"); continue; }
    const type = checker.getNonNullableType(checker.getTypeAtLocation(expression));
    if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown) || type.getStringIndexType()) {
      fail(call.where + ": query keys are not statically bounded");
    } else {
      for (const member of type.getProperties()) {
        queryChecks++;
        if (!route.query.includes(member.name)) fail(call.where + " " + call.path + ": unknown query field " + member.name);
      }
    }
  }
  // Field presence is checked where both sides declare structured responses.
  // This is not runtime JSON validation or a claim about all field value types.
  const returnName = route.returns.replace(/^list\[(.+)\]$/, "$1");
  const backendFields = fieldsOfSchema(returnName);
  if (backendFields && call.wrapper !== "requestText" && call.node.typeArguments?.length === 1) {
    let type = checker.getTypeFromTypeNode(call.node.typeArguments[0]);
    if (checker.isArrayType(type)) type = checker.getTypeArguments(type)[0];
    for (const member of type.getProperties()) {
      if (member.flags & ts.SymbolFlags.Optional) continue;
      responseChecks++;
      if (!(member.name in backendFields)) fail(call.where + " " + call.path + ": response field absent from backend " + member.name);
    }
  }
}
// Require each supported server publisher shape to provide the client's required
// fields. Optional event fields and nested payload/value types are outside this
// presence gate; executed socket tests cover dispatch/ownership separately.
const socket = program.getSourceFile(APP_DIR + "src/realtime/socket.ts");
if (!socket) throw new Error("Socket source not in TypeScript program");
for (const [eventName, interfaceName] of [["message", "MessageSocketEvent"], ["poll", "PollSocketEvent"]]) {
  const declaration = socket.statements.find(node => ts.isInterfaceDeclaration(node) && node.name.text === interfaceName);
  const publishers = inventory.events[eventName];
  if (!declaration || !publishers?.length) {
    fail("Missing supported WebSocket contract: " + eventName);
    continue;
  }
  const type = checker.getTypeAtLocation(declaration);
  for (const member of type.getProperties()) {
    if (member.flags & ts.SymbolFlags.Optional) continue;
    eventChecks++;
    if (publishers.some(fields => !fields.includes(member.name))) fail(eventName + ": WebSocket field absent from backend " + member.name);
  }
}
for (const unresolvedCall of unresolved) fail("UNRESOLVED " + unresolvedCall);
if (calls.length < 40) fail("Extractor collapsed: " + calls.length + " calls, expected at least 40");
const csvCalls = calls.filter(call => call.wrapper === "requestText");
if (csvCalls.length < 2) fail("requestText coverage collapsed: expected both CSV callers");
console.log(`${calls.length} actual HTTP calls (${csvCalls.length} text), ${queryChecks} query fields, ${responseChecks} structured response fields, ${eventChecks} required WebSocket fields, ${unresolved.length} unresolved`);
console.log(failures ? `${failures} FAILURE(S)` : "ALL PASS");
process.exit(failures ? 1 : 0);
