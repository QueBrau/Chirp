/**
 * Verifies every app-mobile/src/api/*.ts request() call against a real backend
 * route — method AND path, not just that the code compiles.
 *
 *   npm run verify:contract
 *
 * Board card c121. c115 shipped broken for days because the like button called
 * POST /posts/{post_id}/likes and the backend only ever registered PUT. tsc
 * cannot catch that: the HTTP verb is a string literal on both sides, and mobile
 * has no integration test that actually asks the backend whether it agrees.
 *
 * This does not boot FastAPI, does not need Python, and does not need a live
 * server. Both sides are read as plain text and compared statically, same
 * philosophy as scripts/board-check: cheap, offline, fails loud. The backend
 * job already asserts routes work; this asserts the CLIENT is asking for the
 * right ones, which is the half nothing else checks.
 *
 * Scope is deliberately one-directional: for every client call, does a
 * matching backend route exist? A backend route the client never calls is not
 * a bug this script cares about - that's unused API surface, not a broken one.
 */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const API_DIR = new URL("../src/api/", import.meta.url);
const ROUTERS_DIR = new URL("../../backend/app/routers/", import.meta.url);

// client.ts is the request() wrapper itself, not a caller. index.ts only
// re-exports the other files - scanning it would double-count every call.
const SKIP_API_FILES = new Set(["client.ts", "index.ts"]);

const WILDCARD = "*";

/** Split a normalized path into segments, turning any param slot into WILDCARD. */
function segments(path) {
  return path.split("/").filter((s) => s.length > 0);
}

/** Backend: "/posts/{post_id}/likes" -> ["posts", "*", "likes"]. */
function backendSegments(literalPath) {
  return segments(literalPath).map((s) => (/^\{[^}]+\}$/.test(s) ? WILDCARD : s));
}

/**
 * Client template literal source (between backticks, or a plain quoted
 * string) -> segments, with each ${...} interpolation collapsed to WILDCARD
 * BEFORE splitting on "/". Every real call site in this codebase puts one
 * interpolation per path segment (e.g. `/chapters/${chapterId}/members`) -
 * never two in one segment, never a literal "/" inside one - so collapsing
 * each ${...} run to a single placeholder character before splitting is exact
 * for everything that exists today, and a mismatch here would show up as a
 * segment-count difference, which still fails loud rather than matching
 * something it shouldn't.
 */
function clientSegments(raw) {
  const collapsed = raw.replace(/\$\{[^}]*\}/g, "\u0000");
  return segments(collapsed).map((s) => (s === "\u0000" ? WILDCARD : s));
}

function sameShape(a, b) {
  if (a.length !== b.length) return false;
  return a.every((seg, i) => seg === WILDCARD || b[i] === WILDCARD || seg === b[i]);
}

/** Scan `text` from `openParenIndex` (the "(" itself) and return the index one
 * past its matching ")", respecting nesting and skipping over string/template
 * literals so a stray "(" or ")" inside a string can't desync the count. */
function matchParen(text, openParenIndex) {
  let depth = 0;
  let i = openParenIndex;
  for (; i < text.length; i++) {
    const ch = text[i];
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      i++;
      while (i < text.length && text[i] !== quote) {
        if (text[i] === "\\") i++;
        i++;
      }
      continue;
    }
    if (ch === "(") depth++;
    else if (ch === ")") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  throw new Error("unbalanced parentheses starting at " + openParenIndex);
}

/** The first string/template literal in a request()-call's argument text -
 * i.e. the path argument - or null if the call doesn't open with one. */
function firstLiteral(argText) {
  const m = /^\s*(['"`])/.exec(argText);
  if (!m) return null;
  const quote = m[1];
  let i = m.index + m[0].length;
  const start = i;
  while (i < argText.length && argText[i] !== quote) {
    if (argText[i] === "\\") i++;
    i++;
  }
  return { raw: argText.slice(start, i), isTemplate: quote === "`" };
}

// --- backend: every @router.<verb>("...") across routers/*.py, verb+path only ---

const ROUTE_DECORATOR = /@router\.(get|post|put|patch|delete)\(/g;
const backendRoutes = []; // { method, segments, path, file }

for (const file of readdirSync(ROUTERS_DIR)) {
  if (!file.endsWith(".py")) continue;
  const text = readFileSync(new URL(file, ROUTERS_DIR), "utf8");
  for (const m of text.matchAll(ROUTE_DECORATOR)) {
    const openParen = m.index + m[0].length - 1;
    const closeParen = matchParen(text, openParen);
    const argText = text.slice(openParen + 1, closeParen - 1);
    const lit = firstLiteral(argText);
    if (!lit) continue; // no route in this codebase has a computed path (verified by hand); skip rather than guess
    backendRoutes.push({
      method: m[1].toUpperCase(),
      segments: backendSegments(lit.raw),
      path: lit.raw,
      file,
    });
  }
}

// --- client: every request<...>(...) across api/*.ts (excluding the wrapper itself) ---

// requestWithHeaders (board c102) is the same call shape as request/requestText —
// it just also hands back the raw Headers — so it must be recognized here too, or
// its one caller (listPosts) silently drops out of contract coverage.
const REQUEST_CALL = /\brequest(?:WithHeaders)?\s*(?=<|\()/g;

function skipGeneric(text, i) {
  if (text[i] !== "<") return i;
  let depth = 0;
  for (; i < text.length; i++) {
    if (text[i] === "<") depth++;
    else if (text[i] === ">") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  throw new Error("unbalanced <> starting near " + i);
}

const clientCalls = []; // { method, segments, path, file, line }
const unresolved = []; // calls skipped because the path wasn't a literal - reported, not failed on

for (const file of readdirSync(API_DIR)) {
  if (!file.endsWith(".ts") || SKIP_API_FILES.has(file)) continue;
  const text = readFileSync(new URL(file, API_DIR), "utf8");
  for (const m of text.matchAll(REQUEST_CALL)) {
    let i = skipGeneric(text, m.index + m[0].length);
    if (text[i] !== "(") continue; // "request" matched something that isn't a call (e.g. a comment)
    const closeParen = matchParen(text, i);
    const argText = text.slice(i + 1, closeParen - 1);
    const line = text.slice(0, m.index).split("\n").length;
    const lit = firstLiteral(argText);
    if (!lit) {
      unresolved.push({ file, line });
      continue;
    }
    const methodMatch = /method:\s*"([A-Z]+)"/.exec(argText);
    const method = methodMatch ? methodMatch[1] : "GET"; // matches doFetch's `options.method ?? "GET"`
    clientCalls.push({
      method,
      segments: lit.isTemplate ? clientSegments(lit.raw) : segments(lit.raw),
      path: lit.raw,
      file,
      line,
    });
  }
}

// --- diff ---

let failures = 0;
console.log(`checked ${clientCalls.length} client call(s) against ${backendRoutes.length} backend route(s)\n`);

for (const call of clientCalls) {
  const match = backendRoutes.find((r) => r.method === call.method && sameShape(call.segments, r.segments));
  if (match) {
    console.log(`  PASS  ${call.method.padEnd(6)} ${call.path}  (${call.file}:${call.line})`);
  } else {
    failures++;
    console.log(`  FAIL  ${call.method.padEnd(6)} ${call.path}  (${call.file}:${call.line})`);
    console.log(`        no backend route matches ${call.method} ${call.segments.join("/")}`);
    const sameShapeDifferentMethod = backendRoutes.filter((r) => sameShape(call.segments, r.segments));
    if (sameShapeDifferentMethod.length > 0) {
      console.log(
        `        this path IS registered, but only as: ${sameShapeDifferentMethod
          .map((r) => r.method)
          .join(", ")} (in ${sameShapeDifferentMethod[0].file})`,
      );
    }
  }
}

if (unresolved.length > 0) {
  console.log(`\n${unresolved.length} call(s) skipped, path argument was not a literal - check by hand:`);
  for (const u of unresolved) console.log(`  ? ${u.file}:${u.line}`);
}

// Board card c137, found by security's post-c103 vacuous-gate sweep, same shape as c103
// itself: `failures` only ever increments inside the loop over clientCalls above. If
// extraction finds nothing - the regex misses a new call style, src/api/ gets renamed,
// request() gets wrapped in a helper - clientCalls is empty, the loop never runs,
// failures stays 0, and this prints ALL PASS having verified nothing. Proven by security:
// a scratch tree with real backend routers and one client file in an unrecognised call
// style produced "checked 0 client call(s) ... ALL PASS ... EXIT CODE: 0".
//
// MIN_CLIENT_CALLS is not a guess - it is comfortably below the real count (78 today,
// 88 backend routes) so ordinary API growth or shrinkage never trips it, while a
// collapsed extraction (0, or anything near it) always does.
const MIN_CLIENT_CALLS = 40; // real count today is 78
if (clientCalls.length < MIN_CLIENT_CALLS) {
  console.log(`\nEXTRACTOR FLOOR: only ${clientCalls.length} client call(s) found, expected >= ${MIN_CLIENT_CALLS}.`);
  console.log("The extractor is probably broken, not the contract - fix the regex, do not lower the floor.");
  process.exit(1);
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
