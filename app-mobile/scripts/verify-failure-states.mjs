/**
 * Regression trap for the c313/c316 failure-state fixes (board card c318).
 *
 *   npm run verify:failure-states
 *
 * Those fixes were proven against a live proxy and merged with no committed check, so
 * nothing stopped a refactor from quietly reverting them. What makes them fragile is
 * that the load-bearing property is ORDER, not presence: every gate can still exist,
 * every string can still be in the file, and the screen can still lie - because a
 * later-but-more-specific gate is being reached first.
 *
 * The three lies these guard against, stated so a future reader knows what is at stake:
 *   dues.tsx        a failed membership check rendering "Join a chapter first" to a
 *                   paying member - and, worse, a failed payments-status check
 *                   UNLOCKING the pay flow.
 *   dues-plans.tsx  "No dues cycle yet - open one" said to a treasurer on a blip, which
 *                   is an instruction to create a duplicate of a cycle that may exist.
 *   secretary.tsx   "Secretary/president only" said to an actual secretary, i.e. the app
 *                   telling someone their role was revoked because the wifi dropped.
 *
 * ASSERTIONS ARE COMPUTED OR POSITIONAL, NEVER "the string is present". A presence check
 * passes on a file where the gate exists but is unreachable, which is the exact failure
 * mode. And every anchor is SYNTAX, never prose - c312's order check matched a comment
 * containing the phrase it was looking for and failed on correct code.
 */
import { readFileSync } from "node:fs";

const read = (rel) => readFileSync(new URL(`../${rel}`, import.meta.url), "utf8");

const DUES = "app/(tabs)/chapter/dues.tsx";
const PLANS = "app/(tabs)/chapter/dues-plans.tsx";
const SECRETARY = "app/(tabs)/chapter/secretary.tsx";
const MEMBER = "app/(tabs)/chapter/member/[id].tsx";

const sources = {
  [DUES]: read(DUES),
  [PLANS]: read(PLANS),
  [SECRETARY]: read(SECRETARY),
  [MEMBER]: read(MEMBER),
};

let failures = 0;
const pass = (name) => console.log(`  PASS  ${name}`);
const fail = (name, detail) => {
  console.log(`  FAIL  ${name}`);
  if (detail) console.log(`        ${detail}`);
  failures++;
};

/** Index of an exact syntax anchor, or -1. Never anchor on prose. */
function at(file, anchor) {
  const idx = sources[file].indexOf(anchor);
  if (idx === -1) fail(`anchor missing in ${file}: ${anchor}`, "the gate itself is gone");
  return idx;
}

/**
 * The core assertion: `earlier` must be REACHED before `later`.
 *
 * Both are early-return gates in the same component, so source order is evaluation
 * order. Reordering them is the whole regression this file exists to catch, and it is
 * invisible to any check that only asks whether both gates exist.
 */
function gateOrder(file, earlier, later, why) {
  const a = at(file, earlier);
  const b = at(file, later);
  if (a === -1 || b === -1) return;
  const name = `${file}: "${earlier}" is reached before "${later}"`;
  if (a < b) pass(`${name} — ${why}`);
  else fail(name, `${earlier} at ${a}, ${later} at ${b} — reversed, so ${why} is now false`);
}

/** Evaluate a real expression lifted out of the source, with `status` injected. */
function evaluateWithStatus(expression, status) {
  return new Function("status", `return (${expression});`)(status);
}

/** Pull the argument out of `fn(<expr>);` in the real source. */
function argumentOf(file, fn) {
  const src = sources[file];
  const start = src.indexOf(`${fn}(`);
  if (start === -1) {
    fail(`${file}: could not find ${fn}(...)`, "the assignment this checks is gone");
    return null;
  }
  const open = start + fn.length;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "(") depth++;
    else if (src[i] === ")") {
      depth--;
      if (depth === 0) return src.slice(open + 1, i);
    }
  }
  fail(`${file}: unbalanced parens reading ${fn}(...)`);
  return null;
}

console.log("\n-- dues.tsx: the money screen --");

gateOrder(
  DUES,
  "if (membershipFailed) {",
  "if (membership === undefined) {",
  "a failed check is not a slow one",
);
gateOrder(
  DUES,
  "if (membership === undefined) {",
  "if (membership === null) {",
  "still-loading is not not-a-member",
);
gateOrder(
  DUES,
  "if (membershipFailed) {",
  "if (membership === null) {",
  "a paying member on bad wifi must never be told to join a chapter",
);

// THE MONEY-SAFETY ASSERTION, and the reason it is executed rather than matched: the
// difference between `?? false` and `?? true` is two characters, and the difference
// between failing closed and unlocking a payment flow for someone whose chapter may not
// be onboarded at all. Running the real expression is the only check that cannot be
// satisfied by something that merely looks careful.
const acceptsExpr = argumentOf(DUES, "setAcceptsPayments");
if (acceptsExpr !== null) {
  console.log(`   setAcceptsPayments(${acceptsExpr.trim()})`);
  let closed;
  try {
    closed = evaluateWithStatus(acceptsExpr, null);
  } catch (error) {
    closed = `threw ${error.constructor.name}`;
  }
  if (closed === false) {
    pass("dues.tsx: a FAILED payments-status check leaves the pay flow locked (fail closed)");
  } else {
    fail(
      "dues.tsx: a failed payments-status check must fail CLOSED",
      `status=null produced ${JSON.stringify(closed)}, expected false — this unlocks the pay flow on a network error`,
    );
  }

  // The other half: failing closed is worthless if it also fails closed on success.
  const open = evaluateWithStatus(acceptsExpr, { onboarded: true });
  if (open === true) pass("dues.tsx: an onboarded chapter still unlocks the pay flow");
  else fail("dues.tsx: an onboarded chapter must unlock the pay flow", `got ${JSON.stringify(open)}`);
}

// statusKnown is what lets the copy say "couldn't check" instead of "your treasurer
// hasn't set this up" - two different sentences to the same member, only one of them true.
const knownExpr = argumentOf(DUES, "setStatusKnown");
if (knownExpr !== null) {
  const unknown = evaluateWithStatus(knownExpr, null);
  const known = evaluateWithStatus(knownExpr, { onboarded: false });
  if (unknown === false && known === true) {
    pass("dues.tsx: statusKnown separates couldn't-check from not-onboarded");
  } else {
    fail(
      "dues.tsx: statusKnown must distinguish a failed check from a real answer",
      `status=null -> ${JSON.stringify(unknown)} (want false), status={onboarded:false} -> ${JSON.stringify(known)} (want true)`,
    );
  }
}

console.log("\n-- dues-plans.tsx: the duplicate-cycle bait --");
gateOrder(
  PLANS,
  "if (loadFailed) {",
  "if (cycle === undefined) {",
  '"No dues cycle yet - open one" must never be shown on a blip',
);

console.log("\n-- secretary.tsx: the revoked-role lie --");
gateOrder(
  SECRETARY,
  "if (loadFailed) {",
  "if (membership === null) {",
  '"Secretary/president only" must never be shown to an actual secretary',
);

console.log("\n-- member/[id].tsx: the they-left lie (c316) --");

// Ternary arms rather than early returns, but the trap is identical: `member` is null in
// BOTH the failed and the not-found state, so whichever arm is written first wins.
gateOrder(
  MEMBER,
  "loadFailed ? (",
  "member === null ? (",
  '"This member may have left the chapter" must never be shown on a dropped request',
);

// Executed, not matched, for the same reason as the pay-flow default: the difference
// between classifying by status and classifying by "any error" is invisible in a diff
// and total in effect.
//
// NOTE THE DELIBERATE DIVERGENCE FROM c312: the event screen treats 404 AND 403 as
// "genuinely not yours", because an uninvited viewer gets a 403 there. This screen keys
// on 404 ALONE - the manager verified that difference is correct rather than an
// oversight. Do not "harmonise" them; assert what each screen actually needs.
{
  const src = sources[MEMBER];
  const start = src.indexOf("const notAMember =");
  const end = src.indexOf(";", start);
  const expression = start === -1 ? null : src.slice(start + "const notAMember =".length, end);
  if (expression === null) {
    fail(`${MEMBER}: could not find the notAMember classification`);
  } else {
    console.log(`   notAMember =${expression}`);
    class ApiError extends Error {
      constructor(status) {
        super("api");
        this.status = status;
      }
    }
    const evaluate = (error) =>
      new Function("ApiError", "error", `return (${expression});`)(ApiError, error);

    const cases = [
      ["a 404 is a real not-a-member", new ApiError(404), true],
      ["a 500 is OUR failure, not their departure", new ApiError(500), false],
      ["a transport error is not a departure either", new TypeError("network"), false],
      ["a 403 is not treated as a departure on this screen", new ApiError(403), false],
    ];
    for (const [name, error, want] of cases) {
      let got;
      try {
        got = evaluate(error);
      } catch (thrown) {
        got = `threw ${thrown.constructor.name}`;
      }
      if (got === want) pass(`member/[id].tsx: ${name}`);
      else fail(`member/[id].tsx: ${name}`, `got ${JSON.stringify(got)}, expected ${want}`);
    }
  }
}

// And the wiring: classifying correctly is useless if loadFailed is set from the wrong
// side of it.
if (sources[MEMBER].includes("setLoadFailed(!notAMember)")) {
  pass("member/[id].tsx: loadFailed is set from the NEGATION - a 404 does not raise it");
} else {
  fail(
    "member/[id].tsx: loadFailed must be set to !notAMember",
    "a 404 raising loadFailed would show the server-error copy for a genuine departure, and vice versa",
  );
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
