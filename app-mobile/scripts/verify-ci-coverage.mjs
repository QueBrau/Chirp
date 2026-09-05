/**
 * Every verifier in this directory actually runs in CI (board c336).
 *
 *   npm run verify:ci-coverage
 *
 * Eight of the fourteen guards here were written with their cards, never wired into
 * ci.yml, and therefore asserted nothing on any pull request. They passed when run by
 * hand, which is how they kept looking healthy: c333's failure-state trap was declared
 * "trapped, not closed" while nothing ran it. A guard nobody runs is documentation with
 * an exit code.
 *
 * This is the same rule the a11y sweep in verify-a11y-touch.mjs uses, turned on the
 * suite itself: NOTHING IS ENUMERATED BY HAND. The scripts are discovered from disk, so
 * a new verifier is covered the moment it exists and a new verifier that nobody wires
 * fails HERE rather than sitting quietly unused. Adding a file is the registration.
 *
 * It deliberately does not check that the guards PASS - CI running them does that. It
 * checks only that CI would notice if they failed.
 */
import { readFileSync, readdirSync } from "node:fs";

const ROOT = new URL("..", import.meta.url).pathname;
const CI = new URL("../../.github/workflows/ci.yml", import.meta.url).pathname;

const scripts = readdirSync(`${ROOT}/scripts`)
  .filter((f) => /^verify-.+\.mjs$/.test(f))
  .map((f) => f.replace(/^verify-|\.mjs$/g, ""))
  .sort();

const pkg = JSON.parse(readFileSync(`${ROOT}/package.json`, "utf8")).scripts ?? {};
const declared = Object.keys(pkg)
  .filter((k) => k.startsWith("verify:"))
  .map((k) => k.slice("verify:".length))
  .sort();
const ci = readFileSync(CI, "utf8");

let failures = 0;
const fail = (msg) => {
  console.log(`  FAIL  ${msg}`);
  failures++;
};

// Discovery that finds nothing must not read as nothing being wrong.
if (scripts.length < 5) {
  console.error(`FAIL  only ${scripts.length} verifiers discovered; this check is looking in the wrong place`);
  process.exit(1);
}

const missingDecl = scripts.filter((s) => !declared.includes(s));
const missingCi = scripts.filter((s) => !new RegExp(`npm run verify:${s}\\b`).test(ci));
// A declared-but-absent script is a stale entry that would fail CI with a confusing
// "missing script" rather than a real result.
const orphans = declared.filter((d) => !scripts.includes(d));

console.log(`  ${scripts.length} verifiers on disk: ${scripts.join(", ")}`);

if (missingDecl.length > 0) fail(`no package.json script: ${missingDecl.join(", ")}`);
else console.log("  PASS  every verifier has an npm script");

if (missingCi.length > 0) {
  fail(`never runs in CI: ${missingCi.join(", ")}`);
  console.log("        add a step to .github/workflows/ci.yml, or delete the guard if it is dead");
} else console.log("  PASS  every verifier runs in ci.yml");

if (orphans.length > 0) fail(`package.json declares a verifier with no file: ${orphans.join(", ")}`);
else console.log("  PASS  no npm script points at a missing verifier");

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
