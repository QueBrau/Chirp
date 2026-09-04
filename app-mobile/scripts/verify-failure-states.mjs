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
import { readdirSync, readFileSync } from "node:fs";

const read = (rel) => readFileSync(new URL(`../${rel}`, import.meta.url), "utf8");

const DUES = "app/(tabs)/chapter/dues.tsx";
const PLANS = "app/(tabs)/chapter/dues-plans.tsx";
const SECRETARY = "app/(tabs)/chapter/secretary.tsx";
const MEMBER = "app/(tabs)/chapter/member/[id].tsx";
const NEW_DM = "app/(tabs)/messages/new.tsx";
const THREAD = "app/(tabs)/messages/[id].tsx";
const PROFILE = "app/(tabs)/profile/index.tsx";

const sources = {
  [DUES]: read(DUES),
  [PLANS]: read(PLANS),
  [SECRETARY]: read(SECRETARY),
  [MEMBER]: read(MEMBER),
  [NEW_DM]: read(NEW_DM),
  [THREAD]: read(THREAD),
  [PROFILE]: read(PROFILE),
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

console.log("\n-- messages/new.tsx: the roster (c317) --");

gateOrder(
  NEW_DM,
  "loadFailed ? (",
  "roster.length === 0 ? (",
  '"No one to add yet" must never be shown because the roster fetch failed',
);

// `!loadFailed &&` in the loading expression LOOKS deletable - it reads like a
// redundant guard next to the membership/members checks. It is not: on failure
// `members` stays null, so without that term the condition stays true forever and the
// screen spins on "Loading roster..." instead of ever reaching the error branch below
// it. Executed rather than matched, because its absence is invisible in a diff.
{
  const src = sources[NEW_DM];
  const start = src.indexOf("const loading =");
  const end = src.indexOf(";", start);
  if (start === -1) {
    fail(`${NEW_DM}: could not find the loading expression`);
  } else {
    const expression = src.slice(start + "const loading =".length, end);
    const evaluate = (loadFailed, sessionStatus, membership, members) =>
      new Function(
        "loadFailed",
        "sessionStatus",
        "membership",
        "members",
        `return (${expression});`,
      )(loadFailed, sessionStatus, membership, members);

    const stuck = evaluate(true, "loaded", { chapter_id: "c" }, null);
    if (stuck === false) {
      pass("messages/new.tsx: a FAILED roster load leaves the loading gate (no infinite spinner)");
    } else {
      fail(
        "messages/new.tsx: a failed roster load must exit the loading gate",
        `loadFailed=true still computed loading=${JSON.stringify(stuck)} - the screen spins forever and the error branch is unreachable`,
      );
    }
    // The other direction, so the guard cannot be satisfied by never loading at all.
    const genuinely = evaluate(false, "loaded", { chapter_id: "c" }, null);
    if (genuinely === true) pass("messages/new.tsx: a genuinely in-flight roster still shows loading");
    else fail("messages/new.tsx: an in-flight roster must still show loading", `got ${JSON.stringify(genuinely)}`);
  }
}

console.log("\n-- messages/[id].tsx: a DIFFERENT shape, asserted differently --");

// HEADS-UP FROM THE AUTHOR, AND THE REASON THIS SECTION EXISTS SEPARATELY: this screen
// is NOT an if-chain. The error renders ALONGSIDE the bubble list so the composer stays
// reachable while it is showing. Reusing the gate-order form here would have passed
// vacuously - the anchors simply would not be found in that shape, and a check that
// cannot fail is worse than no check, because it reports coverage it does not have.
{
  const src = sources[THREAD];
  const blockAt = src.indexOf("{loadFailed ? (");
  const listAt = src.indexOf("messages.map(");
  if (blockAt === -1) {
    fail(`${THREAD}: the inline loadFailed block is gone`);
  } else {
    pass("messages/[id].tsx: the error renders inline, not as an early return");
  }
  if (blockAt !== -1 && listAt !== -1 && blockAt < listAt) {
    pass("messages/[id].tsx: it renders ALONGSIDE the bubble list (composer stays reachable)");
  } else {
    fail(
      "messages/[id].tsx: the error must render alongside the list, not replace it",
      `loadFailed block at ${blockAt}, message list at ${listAt}`,
    );
  }
  // An early return would be the regression that turns this into the other shape.
  if (/if \(loadFailed\)\s*\{?\s*return/.test(src)) {
    fail(
      "messages/[id].tsx: an early return on loadFailed hides the composer",
      "this screen must keep the composer reachable while the error shows",
    );
  } else {
    pass("messages/[id].tsx: no early return on loadFailed");
  }
}

// The catch must live INSIDE load(), not at the call site: the retry action invokes
// load() directly, so a catch attached only to the mount effect leaves a failed RETRY
// unhandled - silently, and exactly when the user is already failing.
{
  const src = sources[THREAD];
  const start = src.indexOf("const load = useCallback(");
  const end = src.indexOf("\n  }, [", start);
  const body = start === -1 || end === -1 ? "" : src.slice(start, end);
  if (body.includes("setLoadFailed(true)")) {
    pass("messages/[id].tsx: the catch lives inside load(), so a failed RETRY is handled too");
  } else {
    fail(
      "messages/[id].tsx: setLoadFailed(true) must be inside load()'s own catch",
      "a catch at the call site leaves the retry path unhandled",
    );
  }
  if (/onAction=\{\(\) => void load\(\)\}/.test(src)) {
    pass("messages/[id].tsx: the retry actually re-runs load()");
  } else {
    fail("messages/[id].tsx: the error state must offer a working retry");
  }
}

console.log("\n-- profile/index.tsx: the third instance (c319) --");

{
  const src = sources[PROFILE];
  const gateAt = src.indexOf("alumniLoadFailed ? (");
  const addAt = src.indexOf('"Add your company"');
  if (gateAt !== -1 && addAt !== -1 && gateAt < addAt) {
    pass('profile: a failed alumni load is reached before "Add your company"');
  } else {
    fail(
      'profile: a failed alumni load must not render "Add your company"',
      "an alum who filled this in months ago would be told they never had, and invited to retype it",
    );
  }
  const start = src.indexOf("const loadAlumniProfile = useCallback(");
  const end = src.indexOf("\n  }, [", start);
  const body = start === -1 || end === -1 ? "" : src.slice(start, end);
  if (body.includes("setAlumniLoadFailed(true)")) {
    pass("profile: the catch lives inside loadAlumniProfile(), so the retry is handled");
  } else {
    fail("profile: setAlumniLoadFailed(true) must be inside loadAlumniProfile()'s own catch");
  }
}

console.log("\n-- the phrase trap --");

// "matches the repo pattern elsewhere in this stack" is 3-for-3 as a marker for this
// defect class, because it cites the bug's OWN SPREAD as its justification. Cheaper and
// faster than any AST rule at catching the next copy-paste.
//
// QUOTED OCCURRENCES ARE ALLOWED, AND THE DISTINCTION IS THE POINT. Two files now quote
// the sentence in comments that name it as the anti-pattern it is - that is institutional
// memory doing its job, and banning it outright would delete the record of why. An
// UNQUOTED occurrence is the sentence being USED as a warrant, which is the defect. The
// three real instances split cleanly along exactly that line.
{
  const PHRASE = "matches the repo pattern elsewhere in this stack";
  const roots = ["app", "src"];
  const offenders = [];
  for (const root of roots) {
    const entries = readdirSync(new URL(`../${root}`, import.meta.url), {
      recursive: true,
      withFileTypes: true,
    });
    for (const entry of entries) {
      if (!entry.isFile() || !/\.(ts|tsx)$/.test(entry.name)) continue;
      const full = `${entry.parentPath ?? entry.path}/${entry.name}`;
      const text = readFileSync(full, "utf8");
      if (!text.includes(PHRASE)) continue;
      for (const [i, line] of text.split("\n").entries()) {
        if (!line.includes(PHRASE)) continue;
        const quoted = line.includes(`"${PHRASE}"`) || line.includes(`\u201c${PHRASE}\u201d`);
        if (!quoted) offenders.push(`${full.split("/app-mobile/")[1] ?? full}:${i + 1}`);
      }
    }
  }
  if (offenders.length === 0) {
    pass("no file under app/ or src/ USES the phrase as a justification (quoted citations are fine)");
  } else {
    fail(
      "the phrase is being used as a justification again",
      `${offenders.join(", ")} - it cites the defect's own spread as its warrant; fix the fail-soft, do not delete the comment`,
    );
  }
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
