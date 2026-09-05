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
 * KNOWN GAP, carded rather than built: the generic collapse scan below models the lie as
 * a BRANCH (an empty state reached when a fetch result is null). At least one member of
 * this class is not a branch at all — profile's post count rendered a fabricated 0 inline,
 * `{postCountFailed ? "—" : count}`, where a number always renders. That file is covered
 * by a named check above instead. If a second value-swap instance ever appears, the
 * generic discriminator is presence in the SAME expression that renders the value, not
 * position before a branch — worth building then, not for one instance now.
 *
 * HOW TO FALSIFY A CHECK IN THIS FILE — a procedure, not a habit.
 *
 * RESTORE THE ACTUAL PRIOR STATE. `git show origin/main:<path> > <path>`, run, restore.
 * Do NOT hand-build something that resembles the bug: the c333 pass sabotaged a failure
 * flag by renaming it `_unusedTreeFailed`, which still contained "Failed", so the check
 * could not fire and the falsification proved nothing. A stand-in you write is a
 * stand-in you write to be caught; the real prior state is the thing that got past you.
 *
 * Then check the check itself, because a green sabotage run has two explanations and
 * only one of them is good: either the assertion is weak, or the sabotage never landed.
 * Read the offender list, not just the pass/fail count — the c333 pass had a check that
 * named ONE of the two files it was written for and looked healthy at the summary line.
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
const PRESIDENT = "app/(tabs)/chapter/president.tsx";
const NODE_DETAIL = "src/tree/NodeDetail.tsx";

const sources = {
  [DUES]: read(DUES),
  [PLANS]: read(PLANS),
  [SECRETARY]: read(SECRETARY),
  [MEMBER]: read(MEMBER),
  [NEW_DM]: read(NEW_DM),
  [THREAD]: read(THREAD),
  [PROFILE]: read(PROFILE),
  [PRESIDENT]: read(PRESIDENT),
  [NODE_DETAIL]: read(NODE_DETAIL),
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

console.log("\n-- profile/index.tsx: memberships, failed vs ANSWERED (c321) --");

// THE DISTINCTION THIS SECTION EXISTS FOR. Everywhere else in this file an empty result
// was always suspect. Here [] is a REAL answer - a chapter-less student genuinely has no
// memberships, and dropping the orgs/activity sections for them is correct. The defect
// was that a FAILED fetch produced the same [], so a real member's chapter, role and
// activity vanished from their own profile with nothing on screen being untrue.
//
// So these check failed-vs-answered, and deliberately do NOT assert anything about []
// being wrong - an assertion of that shape would fire on every chapter-less user.
{
  const src = sources[PROFILE];

  // The section drop must still key on membership, not on "did the fetch return rows".
  if (/\(section\.key === "orgs" \|\| section\.key === "activity"\) && membership === null/.test(src)) {
    pass("profile: a genuinely chapter-less user still has orgs/activity dropped (empty is an ANSWER)");
  } else {
    fail(
      "profile: the section drop must still key on membership === null",
      "rewriting it around the failure flag would break the chapter-less case, which is the one [] is right for",
    );
  }

  const start = src.indexOf("const loading =");
  const end = src.indexOf(";", start);
  if (start === -1) {
    fail(`${PROFILE}: could not find the loading expression`);
  } else {
    const expression = src.slice(start + "const loading =".length, end);
    const evaluate = (status, user, memberships, membershipsFailed, postCount) =>
      new Function(
        "status",
        "user",
        "memberships",
        "membershipsFailed",
        "postCount",
        `return (${expression});`,
      )(status, user, memberships, membershipsFailed, postCount);

    const failed = evaluate("loaded", {}, null, true, 0);
    if (failed === false) {
      pass("profile: a FAILED memberships fetch leaves the loading gate (no infinite spinner)");
    } else {
      fail(
        "profile: a failed memberships fetch must exit the loading gate",
        `computed loading=${JSON.stringify(failed)} - memberships stays null on failure, so this spins forever`,
      );
    }
    const inflight = evaluate("loaded", {}, null, false, 0);
    if (inflight === true) pass("profile: a genuinely in-flight fetch still shows loading");
    else fail("profile: an in-flight fetch must still show loading", `got ${JSON.stringify(inflight)}`);
  }

  // Second-order: postCount is gated on memberships settling, and "settle" has to
  // include failing - otherwise it stays null and the gate above never opens even once
  // the memberships term is correct. Fixing one half and shipping an infinite spinner
  // is a live risk here, not a hypothetical.
  if (/if \(memberships === null && !membershipsFailed\) return;/.test(src)) {
    pass("profile: postCount does not park forever when the memberships fetch fails");
  } else {
    fail(
      "profile: the postCount effect must treat a FAILED fetch as settled",
      "waiting on `memberships === null` alone leaves postCount null and the loading gate shut",
    );
  }

  if (/membershipsFailed \? \(/.test(src) && /actionLabel="Try again"/.test(src)) {
    pass("profile: the failure is stated on screen with a retry, not left as a silent absence");
  } else {
    fail("profile: a failed memberships fetch must be visible and retryable");
  }

  const loaderStart = src.indexOf("const loadMemberships = useCallback(");
  const loaderEnd = src.indexOf("\n  }, [", loaderStart);
  const body = loaderStart === -1 || loaderEnd === -1 ? "" : src.slice(loaderStart, loaderEnd);
  if (body.includes("setMembershipsFailed(true)")) {
    pass("profile: the catch lives inside loadMemberships(), so the retry is covered");
  } else {
    fail("profile: setMembershipsFailed(true) must be inside loadMemberships()'s own catch");
  }
}

console.log("\n-- president.tsx: three false statements from one failed fetch (c324) --");

{
  const src = sources[PRESIDENT];

  // The roster failure produced a COUNT ("0 on the roster"), a claim ("No members") and
  // an accusation ("Invites you've sent will show up here once redeemed") - the last of
  // which points a president at their own invitations for a network error.
  if (/membersFailed\s*\n?\s*\? "Roster didn't load"/.test(src)) {
    pass("president: the roster caption stops asserting a count when the fetch failed");
  } else {
    fail(
      'president: the caption must not claim "0 on the roster" on a failed fetch',
      "the number is the quietest of the three lies and the easiest to leave behind",
    );
  }

  const errAt = src.indexOf("Couldn't load the roster");
  const emptyAt = src.indexOf('title="No members"');
  if (errAt !== -1 && emptyAt !== -1 && errAt < emptyAt) {
    pass("president: the roster error is reached before the No-members copy");
  } else {
    fail("president: a failed roster must not fall through to the No-members copy");
  }
  // Same principle as c312: stop lying, do not delete the honest copy. A genuinely
  // empty roster is a real state and still deserves its own message.
  if (emptyAt !== -1 && /Invites you've sent will show up here once redeemed/.test(src)) {
    pass("president: the honest empty-roster copy still exists for a genuinely empty roster");
  } else {
    fail("president: the genuine empty-roster copy must survive the fix");
  }

  const start = src.indexOf("const loading =");
  const end = src.indexOf(";", start);
  const expression = start === -1 ? null : src.slice(start + "const loading =".length, end);
  if (expression === null) {
    fail(`${PRESIDENT}: could not find the loading expression`);
  } else {
    const evaluate = (sessionStatus, membership, chapterLoading, members, membersFailed) =>
      new Function(
        "sessionStatus",
        "membership",
        "chapterLoading",
        "members",
        "membersFailed",
        `return (${expression});`,
      )(sessionStatus, membership, chapterLoading, members, membersFailed);

    const failed = evaluate("loaded", {}, false, null, true);
    if (failed === false) {
      pass("president: a FAILED roster leaves the loading gate (no infinite spinner)");
    } else {
      fail(
        "president: a failed roster must exit the loading gate",
        `computed loading=${JSON.stringify(failed)} - members stays null on failure, so this hangs`,
      );
    }
    const inflight = evaluate("loaded", {}, false, null, false);
    if (inflight === true) pass("president: a genuinely in-flight roster still shows loading");
    else fail("president: an in-flight roster must still show loading", `got ${JSON.stringify(inflight)}`);
  }

  const loaderStart = src.indexOf("const refreshMembers = useCallback(");
  const loaderEnd = src.indexOf("\n  }, [", loaderStart);
  const body = loaderStart === -1 || loaderEnd === -1 ? "" : src.slice(loaderStart, loaderEnd);
  if (body.includes("setMembersFailed(true)")) {
    pass("president: the catch lives inside refreshMembers(), so the retry is covered");
  } else {
    fail("president: setMembersFailed(true) must be inside refreshMembers()'s own catch");
  }

  // The two quieter ones. Neither produced a false SENTENCE - the overview panel simply
  // vanished and the identity boxes simply went blank - which is the c321 false-absence
  // symptom and the reason both were triaged LOW before that standard existed.
  const ovFailAt = src.indexOf("overviewFailed ? (");
  const ovNullAt = src.indexOf("overview === null ? null");
  if (ovFailAt !== -1 && ovNullAt !== -1 && ovFailAt < ovNullAt) {
    pass("president: a failed overview says so instead of the panel silently vanishing");
  } else {
    fail("president: a failed overview must be stated, not rendered as absence");
  }

  const chFailAt = src.indexOf("chapterFailed ? (");
  const orgInputAt = src.indexOf("value={orgName}");
  if (chFailAt !== -1 && orgInputAt !== -1 && chFailAt < orgInputAt) {
    pass("president: a failed chapter fetch hides the name fields rather than showing them blank");
  } else {
    fail(
      "president: an empty ORG NAME box reads as 'this chapter has no name set'",
      "traced to render for c324 - never destructive (Save is gated on chapter !== null) but still failure presented as fact",
    );
  }
}

console.log("\n-- NodeDetail + postCount: the c330 survivors --");

{
  const src = sources[NODE_DETAIL];
  if (/setTermsFailed\(!\(error instanceof ApiError && error\.status === 404\)\)/.test(src)) {
    pass("NodeDetail: a 404 is a real no-role, anything else is our failure (c316's classification)");
  } else {
    fail(
      "NodeDetail: must tell a 404 apart from a transport failure",
      "treating every error as no-role is the c316 bug on the same endpoint one screen over",
    );
  }
  const failAt = src.indexOf("termsFailed ? (");
  // Anchored on the FULL rendered string, not the phrase: the c330 comment above the
  // catch quotes "No current role on" mid-sentence, and anchoring on the short form
  // matched the COMMENT at line 68 instead of the JSX at 147 — this check failed on
  // correct code the first time it ran. Second time this trap has fired (c312 was the
  // first), inside the very script that documents it.
  const emptyAt = src.indexOf("No current role on record");
  if (failAt !== -1 && emptyAt !== -1 && failAt < emptyAt) {
    pass("NodeDetail: the failure arm is reached before the no-role copy");
  } else {
    fail("NodeDetail: a failed terms fetch must not render as 'no current role'");
  }
}

{
  const src = sources[PROFILE];
  if (/setPostCountFailed\(true\)/.test(src)) {
    pass("profile: a failed post count is recorded rather than rendered as 0");
  } else {
    fail("profile: setPostCount(0) on failure states a NUMBER as fact - 0 posts is a claim");
  }
  // AND IT MUST BE READ WHERE THE NUMBER RENDERS. Recording the failure and consuming it
  // are different jobs, and this check asserted only the first: deleting both reads while
  // leaving the setter in place left the suite at ALL PASS, with the original fabricated
  // 0 back on screen. Same setter-not-read gap the collapse check was blocked for, in a
  // second place, on one of the two census survivors — found by chirps-ad, who hit the
  // human version of it in their own c317 first cut.
  //
  // THIS SHAPE IS NOT A BRANCH. There is no "No posts" empty state to order against; the
  // lie was a NUMBER rendered where a number always renders, swapped inline. So the
  // discriminator is presence in the same expression, not position before a branch.
  if (/\{postCountFailed\s*\?/.test(src) && /variant="stat">\{postCountFailed/.test(src)) {
    pass("profile: the failure is READ in the expression that renders the count");
  } else {
    fail(
      "profile: postCountFailed must be consumed where the number renders",
      "a setter with nothing reading it puts the fabricated 0 straight back on screen",
    );
  }
  // The c321 second-order lesson, applied to the fix that c321 itself planted: postCount
  // must still be SET on failure or the loading gate never opens. Recording the failure
  // and clearing the gate are two different jobs and both are required.
  if (/postCount === null/.test(src) && /setPostCount\(0\)/.test(src)) {
    pass("profile: the count is still set on failure, so the loading gate still opens");
  } else {
    fail(
      "profile: recording the failure must not re-hang the screen",
      "the gate keys on postCount === null, so the failure path must still set a number",
    );
  }
}

console.log("\n-- NEW SHAPE: null collapsed with empty, under a create action --");

// THE CLASS c333 NAMED, and it is narrower than "failure as fact" on purpose.
//
// A render condition of the form `X === null || X.something.length === 0` folds two
// different facts into one branch: "we do not know" and "there is genuinely nothing".
// Dozens of branches in this app collapse those two and are perfectly fine — right up
// until the branch puts a CREATE button underneath. Then a dropped request does not
// merely misinform, it recruits the user into recording something their chapter may
// already have: "Pair the first big and little" over a lineage that exists, "New
// family" over families that exist, the dues-plans duplicate-cycle shape (c299).
//
// So the rule is not "never collapse null with empty". It is: if you collapse them,
// there must be a failure branch reached BEFORE this one. This check finds every
// collapse in app/ and src/ and requires a *Failed identifier earlier in the file.
{
  const collapse = /(\w+) === null \|\| \1[\w.?[\]]*\.length === 0/g;
  const offenders = [];
  let checked = 0;
  for (const root of ["app", "src"]) {
    const entries = readdirSync(new URL(`../${root}`, import.meta.url), {
      recursive: true,
      withFileTypes: true,
    });
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith(".tsx")) continue;
      const full = `${entry.parentPath ?? entry.path}/${entry.name}`;
      const text = readFileSync(full, "utf8");
      collapse.lastIndex = 0;
      let m;
      while ((m = collapse.exec(text)) !== null) {
        checked++;
        // STRIP COMMENTS FIRST, and require a SETTER CALL rather than the word.
        //
        // The first version of this check tested `before` for /\w*[Ff]ailed/ and passed
        // on historian.tsx — because a comment 40 lines up happened to contain the word
        // "failed" in prose ("roleMeta null (loading or failed) fails"). It reported
        // coverage it did not have, on one of the two survivors this check was written
        // for. Third instance of the anchor-on-prose trap today, this time inside the
        // detector for the class.
        //
        // `set<Something>Failed(` is code and cannot be written in a sentence.
        const before = text
          .slice(0, m.index)
          .replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/\/\/[^\n]*/g, "");
        // THE CREATE-ACTION CLAUSE, and it is what keeps this rule honest rather than
        // a restatement of the whole class. feed/index.tsx collapses null with empty and
        // is CORRECT: its branch is `return null`, so a failed invites fetch simply hides
        // the strip — no copy, no claim, nothing to act on. Flagging it would make this
        // check fire on correct code, which is how a check earns being ignored.
        //
        // What makes tree/historian different is the button underneath: "Pair the first
        // big and little" / "New family" turn a false empty into an instruction to
        // duplicate something that already exists.
        // The collapse is sometimes INLINE in the JSX (historian) and sometimes hoisted
        // into a named const used further down (tree.tsx's `nothingToDraw`). A fixed
        // window from the match only catches the first — it missed tree.tsx, one of the
        // two survivors this check exists for, until this followed the name to its use.
        const declared = text.slice(0, m.index).match(/const (\w+) =\s*$/);
        let branch = text.slice(m.index, m.index + 700);
        if (declared) {
          const useAt = text.indexOf(`${declared[1]} ? (`, m.index);
          if (useAt !== -1) branch = text.slice(useAt, useAt + 900);
        }
        const offersCreate = /actionLabel=/.test(branch);

        // A SETTER IS NOT A FIX. Requiring only /set\w*Failed\(/ above the collapse
        // asserts that a failure flag is WRITTEN somewhere — not that anything READS it
        // before the empty branch. Deleting tree.tsx's entire treeFailed render arm while
        // leaving the state and the catch in place passed this check at exit 0: the
        // no-op-fix shape, state set and never read, sailing through the detector built
        // for this class. That is c317's own historical bug (loadFailed set, no branch
        // reading it) reproduced inside the trap. Caught by the manager's falsification,
        // not by mine.
        //
        // So: derive the flag from the setter and require its READ, positioned before the
        // collapse actually renders — the same reads-in-render assertion the named-file
        // checks above already make, generalized to any file this scan reaches.
        const renderAt = declared && branch !== text.slice(m.index, m.index + 700)
          ? text.indexOf(`${declared[1]} ? (`, m.index)
          : m.index;
        const flags = [...before.matchAll(/set(\w*Failed)\s*\(/g)].map(
          (f) => f[1].charAt(0).toLowerCase() + f[1].slice(1),
        );
        const readBeforeRender = flags.some((flag) => {
          const read = text.indexOf(`${flag} ? (`);
          return read !== -1 && read < renderAt;
        });
        if (offersCreate && !readBeforeRender) {
          const line = before.split("\n").length;
          offenders.push(`${full.split("/app-mobile/")[1] ?? full}:${line} (${m[0]})`);
        }
      }
    }
  }
  if (offenders.length === 0) {
    pass(`every null-collapsed-with-empty branch has a failure branch before it (${checked} checked)`);
  } else {
    fail(
      "a branch collapses 'unknown' with 'genuinely empty' and nothing distinguishes a failure first",
      `${offenders.join("; ")} — a create action under a collapsed null/empty branch, with no failure state READ before it. A setter alone is not a fix: state written and never read renders the empty copy exactly as before.`,
    );
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
