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

console.log("ALL PASS");
