/**
 * Offline regression guard for c89's social-auth boundary, plus a few other
 * auth-flow invariants that landed in this same file over time (c331, c379,
 * and now c381's account-type tap gate below).
 *
 * This is intentionally source-level: native provider credentials and Firebase
 * console setup are external to this repository, so no local test can honestly
 * claim Apple/Google authentication works.  What we can prove here is the
 * security-critical invariant that those buttons never route into onboarding
 * without a credential, and that the email path remains present.
 *
 *   npm run verify:auth
 */

import { readFileSync } from "node:fs";

const ROOT = new URL("../", import.meta.url);
const signIn = readFileSync(new URL("app/(auth)/sign-in.tsx", ROOT), "utf8");
const social = readFileSync(new URL("src/auth/social.ts", ROOT), "utf8");

const required = [
  ["Apple handler", 'handleUnavailableSocialProvider("apple")'],
  ["Google handler", 'handleUnavailableSocialProvider("google")'],
  ["Email path", 'label="Continue with Email"'],
  ["Honest unavailable copy", "not connected in this build yet"],
  ["Provider capability guard", "enabled: false"],
];

for (const [name, needle] of required) {
  if (!signIn.includes(needle) && !social.includes(needle)) {
    console.error(`FAIL  ${name}: missing ${JSON.stringify(needle)}`);
    process.exit(1);
  }
  console.log(`PASS  ${name}`);
}

if (/label="Continue with (Apple|Google)"[^\n]*onPress=\{continueToOnboarding\}/.test(signIn)) {
  console.error("FAIL  social buttons must not bypass authentication into onboarding");
  process.exit(1);
}

if (!signIn.includes("socialAuthUnavailableMessage")) {
  console.error("FAIL  social buttons must use the provider abstraction for their error state");
  process.exit(1);
}

// c331: the code-entry state must offer a resend for the SAME address, and the
// send-failure mapper must render the server's 429 as human copy, not a code.
// Source-level on purpose: the live 429 is rate-limiter state, not something a
// unit test can honestly trigger; what we can pin is that the affordance exists
// and is wired to the send path rather than to "start over with a new address".
const verifyCampus = readFileSync(new URL("app/(auth)/verify-campus.tsx", ROOT), "utf8");
const resendRequired = [
  ["Resend affordance", '"Send a new code"'],
  ["Resend re-uses the send path", "void send(true)"],
  ["Different-address path kept", 'label="Use a different address"'],
  ["429 copy is human", "Give it fifteen minutes"],
];
for (const [name, needle] of resendRequired) {
  if (!verifyCampus.includes(needle)) {
    console.error(`FAIL  ${name}: missing ${JSON.stringify(needle)}`);
    process.exit(1);
  }
  console.log(`PASS  ${name}`);
}

// c379: a successful redeem must publish into the session, or Chirps/the campus
// feed stay locked until a full app restart — redeeming does not change userId,
// which is the only thing the session's campusVerification effect re-runs on, so
// nothing else was ever going to notice the new answer. Positional, not just
// present, for the same reason as the checks above: a presence check would pass
// on a build where the publish call exists but is unreachable.
const sessionProvider = readFileSync(new URL("src/auth/SessionProvider.tsx", ROOT), "utf8");

const plumbing = [
  [
    "SessionProvider.tsx: applyCampusVerification is declared on the context type",
    "applyCampusVerification: (verification: CampusVerificationStatus) => void;",
  ],
  [
    "SessionProvider.tsx: applyCampusVerification reaches the context value",
    "applyCampusVerification,",
  ],
];
for (const [name, needle] of plumbing) {
  if (!sessionProvider.includes(needle)) {
    console.error(`FAIL  ${name}: missing ${JSON.stringify(needle)}`);
    process.exit(1);
  }
  console.log(`PASS  ${name}`);
}

// THE ORDER ASSERTION: applyCampusVerification(status) must be reached only AFTER
// redeem()'s `if (!status.verified)` gate — i.e. only on the success path. A
// regression that hoisted the publish above the gate (or deleted the gate) would
// hand the fail-closed consumer below (useCampusAccess) a status it should never
// have seen produced by the redeem call.
{
  const start = verifyCampus.indexOf("const redeem = async () => {");
  const end = verifyCampus.indexOf("\n  };", start);
  const body = start === -1 || end === -1 ? "" : verifyCampus.slice(start, end);
  const gateAt = body.indexOf("if (!status.verified)");
  const publishAt = body.indexOf("applyCampusVerification(status)");
  if (start === -1 || end === -1) {
    console.error("FAIL  verify-campus.tsx: could not find redeem()'s body");
    process.exit(1);
  }
  if (gateAt === -1) {
    console.error("FAIL  verify-campus.tsx: redeem()'s belt-and-braces !verified gate is gone");
    process.exit(1);
  }
  if (publishAt === -1) {
    console.error(
      "FAIL  verify-campus.tsx: redeem() no longer publishes the redeem result into the session",
    );
    process.exit(1);
  }
  if (gateAt < publishAt) {
    console.log(
      "PASS  verify-campus.tsx: a successful redeem publishes into the session, reached only past the !verified gate",
    );
  } else {
    console.error(
      "FAIL  verify-campus.tsx: applyCampusVerification(status) must be reached AFTER the !verified gate, never before it",
    );
    process.exit(1);
  }
}

// THE FAIL-CLOSED PROOF: the campusVerification effect's catch must still set
// null on a failed lookup, never something truthy — extracted and compared
// exactly, not string-matched, so a rename that leaves the word "null" in a
// nearby comment cannot fake a pass.
{
  const effectStart = sessionProvider.indexOf("getCampusVerification()");
  const effectEnd = sessionProvider.indexOf("}, [userId, sessionGeneration]);", effectStart);
  const body =
    effectStart === -1 || effectEnd === -1 ? "" : sessionProvider.slice(effectStart, effectEnd);
  const catchAt = body.indexOf(".catch(() => {");
  const catchBody = catchAt === -1 ? "" : body.slice(catchAt);
  const setAt = catchBody.indexOf("setCampusVerification(");
  if (effectStart === -1 || effectEnd === -1 || catchAt === -1 || setAt === -1) {
    console.error(
      "FAIL  SessionProvider.tsx: could not find the campusVerification fetch's catch branch",
    );
    process.exit(1);
  }
  const open = setAt + "setCampusVerification(".length;
  const close = catchBody.indexOf(")", open);
  const arg = catchBody.slice(open, close).trim();
  if (arg === "null") {
    console.log(
      "PASS  SessionProvider.tsx: a failed verification lookup fails CLOSED (sets null, never read as verified)",
    );
  } else {
    console.error(
      `FAIL  SessionProvider.tsx: a failed verification lookup must set null, got ${JSON.stringify(arg)}`,
    );
    process.exit(1);
  }
}

// c381: (auth)/account-type.tsx opened with a real-looking selection nobody made -
// the invite-code pre-selection and the plain "student" default both looked
// identical to an actual tap, so Continue submitted a default as if it were a
// choice. Source-level for the same reason as everything else in this file: no
// test here can honestly drive a real tap on this screen, but the wiring that
// makes a tap a PRECONDITION for submitting is a plain, regression-prone
// invariant worth pinning by name.
const accountType = readFileSync(new URL("app/(auth)/account-type.tsx", ROOT), "utf8");

const c381Required = [
  ["The user's own tap is tracked separately from what's displayed", "const hasChosen = chosen !== null;"],
  ["Continue is disabled until a tap happens", "disabled={submitting || !hasChosen}"],
];
for (const [name, needle] of c381Required) {
  if (!accountType.includes(needle)) {
    console.error(`FAIL  ${name}: missing ${JSON.stringify(needle)}`);
    process.exit(1);
  }
  console.log(`PASS  ${name}`);
}

// The c301 invite-code pre-selection must survive the c381 fix - a regression that
// "fixes" the default-submission bug by dropping the pre-selection instead (e.g.
// initialising `chosen` from the invite code) would pass the two checks above while
// silently reintroducing c301: an arriving code no longer influencing what's shown
// at all.
if (!accountType.includes('const selected: AccountType = chosen ?? (inviteCode ? "greek" : "non_greek");')) {
  console.error("FAIL  account-type.tsx: the c301 invite-code pre-selection is gone");
  process.exit(1);
}
console.log("PASS  c301 invite-code pre-selection still present alongside the c381 tap gate");

console.log("ALL PASS");
