/**
 * Verifies that src/lib/contentLimits.ts still agrees with the REAL caps in
 * backend/app/core/validation.py (board cards c245, c251).
 *
 *   npm run verify:content-limits
 *
 * There is no build-time path from Python to the Expo bundle, so the client's copy of
 * these numbers is unavoidably a hand-copy. What is avoidable is a hand-copy that
 * drifts silently: raise MAX_POST_BODY_LENGTH on the server and, without this, the
 * composer keeps enforcing the old number with nothing to notice. Same technique as
 * verify-contract.mjs and verify-ws-url.mjs - read both sides as plain text, no
 * Python, no bundler - and the same lesson as c246: a convention nobody checks is a
 * hope, not a guarantee.
 *
 * BOTH extractions fail loudly when they match nothing. A verifier that finds zero
 * constants and reports ALL PASS is the vacuous green this repo has now been bitten by
 * twice; "I could not find the constants" must never be able to look like "the
 * constants agree."
 */
import { readFileSync } from "node:fs";

const BACKEND = new URL("../../backend/app/core/validation.py", import.meta.url);
const MOBILE = new URL("../src/lib/contentLimits.ts", import.meta.url);

/**
 * Backend caps that exist on purpose with NO mobile counterpart. Each needs a reason,
 * because the alternative - an unexplained allow-list - is how a real cap gets
 * silently skipped. A new backend cap that is neither mirrored nor listed here FAILS,
 * so adding one forces a decision about the client instead of being ignored.
 */
const KNOWN_UNMIRRORED = {
  MAX_REASON_LENGTH:
    "no mobile surface collects a free-text moderation reason - every reason this " +
    "client sends is a preset (REPORT_REASONS, or the literal dismiss string), " +
    "longest 26 chars against a 1000 cap",
  MAX_URL_LENGTH:
    "validate_public_url's cap on stored profile/job URLs, not a composer body - " +
    "no character counter applies to it",
};

const readNumbers = (url, pattern, label) => {
  const source = readFileSync(url, "utf8");
  const found = new Map();
  for (const match of source.matchAll(pattern)) {
    found.set(match[1], Number(match[2].replace(/_/g, "")));
  }
  if (found.size === 0) {
    console.error(`FAIL  extracted ZERO constants from ${label}`);
    console.error("      This is an extraction failure, not agreement - the file moved,");
    console.error("      was renamed, or the declaration style changed. Fix the pattern");
    console.error("      in scripts/verify-content-limits.mjs before trusting this step.");
    process.exit(1);
  }
  return found;
};

const backend = readNumbers(BACKEND, /^(MAX_[A-Z_]+)\s*=\s*([\d_]+)\s*$/gm, "core/validation.py");
const mobile = readNumbers(
  MOBILE,
  /^export const (MAX_[A-Z_]+)\s*=\s*([\d_]+);/gm,
  "src/lib/contentLimits.ts",
);

let failures = 0;
const pass = (msg) => console.log(`  PASS  ${msg}`);
const fail = (msg, detail) => {
  console.log(`  FAIL  ${msg}`);
  if (detail) console.log(`        ${detail}`);
  failures++;
};

console.log(
  `checked ${mobile.size} mobile constant(s) against ${backend.size} backend cap(s)\n`,
);

// 1. Every mobile constant must exist on the backend with the SAME value.
for (const [name, mobileValue] of mobile) {
  if (!backend.has(name)) {
    fail(`${name} exists in the client but NOT in core/validation.py`,
      "the server cap it mirrors was renamed or removed");
    continue;
  }
  const backendValue = backend.get(name);
  if (backendValue !== mobileValue) {
    fail(`${name} disagrees`, `backend ${backendValue}, mobile ${mobileValue}`);
    continue;
  }
  pass(`${name} = ${mobileValue} matches core/validation.py`);
}

// 2. Every backend cap must be mirrored, or knowingly excluded with a reason.
for (const [name, value] of backend) {
  if (mobile.has(name)) continue;
  if (name in KNOWN_UNMIRRORED) {
    pass(`${name} = ${value} deliberately unmirrored (${KNOWN_UNMIRRORED[name]})`);
    continue;
  }
  fail(`${name} = ${value} is a backend cap with no mobile counterpart`,
    "mirror it in src/lib/contentLimits.ts, or add it to KNOWN_UNMIRRORED here with " +
    "the reason a composer does not need it");
}

// --- the boundary itself, run against the REAL exported helpers ---
//
// Matching numbers are only half of it. The client must accept EXACTLY what the
// server accepts: the schemas are Field(max_length=N) against the submitted body,
// and every composer submits body.trim(), so the client has to measure the trimmed
// string and treat exactly N as legal. An off-by-one here, or counting the raw value,
// puts back the after-the-fact 422 this card exists to remove - in the direction that
// is hardest to notice, because it only shows up on a body of exactly the wrong
// length. Same technique as verify-ws-url.mjs: the real source is executed rather
// than a hand-copy of it re-asserted.

const stripTypes = (ts) =>
  ts.replace(/^export /gm, "").replace(/:\s*(?:string|number|boolean)\b/g, "");

let helpers;
try {
  helpers = new Function(
    `${stripTypes(readFileSync(MOBILE, "utf8"))}
     return { charsRemaining, shouldShowCounter, isOverLimit };`,
  )();
} catch (error) {
  console.error("FAIL  could not execute contentLimits.ts helpers");
  console.error(`      ${error.message}`);
  console.error("      (the declarations changed shape? update stripTypes here)");
  process.exit(1);
}
for (const name of ["charsRemaining", "shouldShowCounter", "isOverLimit"]) {
  if (typeof helpers[name] !== "function") {
    console.error(`FAIL  ${name} is not exported from contentLimits.ts any more`);
    process.exit(1);
  }
}

console.log("");
const check = (name, actual, expected) => {
  if (actual === expected) return pass(name);
  fail(name, `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
};

const LIMIT = 2_000;
check("a body of exactly the limit is NOT over (Field(max_length) allows ==)",
  helpers.isOverLimit("x".repeat(LIMIT), LIMIT), false);
check("one character past the limit IS over",
  helpers.isOverLimit("x".repeat(LIMIT + 1), LIMIT), true);
check("trailing whitespace does not count - the server sees body.trim()",
  helpers.isOverLimit(`${"x".repeat(LIMIT)}     `, LIMIT), false);
check("leading whitespace does not count either",
  helpers.isOverLimit(`     ${"x".repeat(LIMIT)}`, LIMIT), false);
check("remaining is 0 exactly at the limit, not -1",
  helpers.charsRemaining("x".repeat(LIMIT), LIMIT), 0);
check("remaining goes negative past the limit",
  helpers.charsRemaining("x".repeat(LIMIT + 7), LIMIT), -7);
check("counter stays hidden on an empty composer",
  helpers.shouldShowCounter("", LIMIT), false);
check("counter stays hidden well short of the limit",
  helpers.shouldShowCounter("x".repeat(LIMIT * 0.5), LIMIT), false);
check("counter appears inside the last tenth",
  helpers.shouldShowCounter("x".repeat(LIMIT - 1), LIMIT), true);
check("counter is still shown once over the limit",
  helpers.shouldShowCounter("x".repeat(LIMIT + 50), LIMIT), true);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
