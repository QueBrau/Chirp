/**
 * Offline regression guard for c89's social-auth boundary.
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

console.log("ALL PASS");
