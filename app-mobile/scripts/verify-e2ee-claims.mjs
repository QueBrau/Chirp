/**
 * Guards the messaging UI's encryption copy against the state of the client
 * crypto (board card c254).
 *
 *   npm run verify:e2ee-claims
 *
 * WHY THIS EXISTS. src/crypto/signal.ts is a typed stub whose every function
 * throws, with zero real callers, while the Messages screens shipped
 * "End-to-end encrypted" and "Direct message - encrypted" as flat present-tense
 * claims. Nothing could be sent (the composer is disabled), so no plaintext was
 * ever at risk - but the app told users a security property it does not have,
 * which is the same family as c76's /terms claiming powers the server lacked.
 *
 * The copy is fixed. This script exists so it cannot drift back, in EITHER
 * direction, because both directions are silent:
 *
 *   - Someone re-adds an encryption claim while the crypto is still a stub.
 *   - Someone ENABLES the composer while the crypto is still a stub, which
 *     would send plaintext under a UI that has a lock icon on it.
 *   - Someone lands real crypto and nobody revisits the deliberately-cautious
 *     copy, so the app under-claims forever.
 *
 * No jest/vitest exists in this repo - see the sibling verify-*.mjs scripts -
 * so this reads the real source files rather than importing them.
 */
import { readFileSync } from "node:fs";

const SIGNAL_TS = new URL("../src/crypto/signal.ts", import.meta.url);
const LIST_SCREEN = new URL("../app/(tabs)/messages/index.tsx", import.meta.url);
const THREAD_SCREEN = new URL("../app/(tabs)/messages/[id].tsx", import.meta.url);

const failures = [];
const fail = (msg, detail) => failures.push(detail ? `${msg}\n      ${detail}` : msg);

/**
 * Every user-visible string in the Messages screens that mentions encryption
 * MUST appear here with the reason it is true today. A string that is not on
 * this list fails the run - so adding a new claim is a deliberate act with a
 * written justification, not an edit nobody reviews.
 */
const ALLOWED_CLAIMS = new Map([
  [
    "Encrypted message",
    "Bubble placeholder for a body this client cannot decrypt. TRUE: the server " +
      "genuinely stores opaque ciphertext (SPEC 6.2/6.5, prekey tables incl kyber); " +
      "the gap is client-side decryption, so 'this is an encrypted message' is honest.",
  ],
  [
    "Sending unlocks with E2EE (milestone 4)",
    "Forward-looking and explicitly not present tense - it tells the user sending " +
      "is unavailable and why. This is the voice the rest of the copy matches.",
  ],
]);

/** Strip comments so prose ABOUT encryption is never mistaken for a UI claim. */
function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/** Quoted string literals plus JSX text nodes - everything a user can read. */
function userVisibleStrings(source) {
  const body = stripComments(source);
  const found = new Set();
  for (const m of body.matchAll(/"((?:[^"\\]|\\.)*)"/g)) found.add(m[1]);
  for (const m of body.matchAll(/'((?:[^'\\]|\\.)*)'/g)) found.add(m[1]);
  for (const m of body.matchAll(/>([^<>{}]+)</g)) {
    const text = m[1].replace(/\s+/g, " ").trim();
    if (text) found.add(text);
  }
  return [...found];
}

const MENTIONS_CRYPTO = /encrypt|e2ee|end.to.end/i;

// ---------------------------------------------------------------------------
// 1. Is the client crypto still a stub?
// ---------------------------------------------------------------------------
const signalSource = readFileSync(SIGNAL_TS, "utf8");
const exportedFns = [...signalSource.matchAll(/export async function (\w+)/g)].map((m) => m[1]);
const todoThrows = [...signalSource.matchAll(/throw new Error\("TODO\(milestone-3\): libsignal"\)/g)];

if (exportedFns.length === 0) {
  fail("could not find any exported functions in src/crypto/signal.ts",
       "(file restructured? update this script's anchors before trusting it)");
}
const cryptoIsStubbed = exportedFns.length > 0 && todoThrows.length === exportedFns.length;

const listSource = readFileSync(LIST_SCREEN, "utf8");
const threadSource = readFileSync(THREAD_SCREEN, "utf8");

if (cryptoIsStubbed) {
  // -------------------------------------------------------------------------
  // 2. Stubbed crypto: the composer must stay disabled.
  // -------------------------------------------------------------------------
  if (!/editable=\{false\}/.test(stripComments(threadSource))) {
    fail(
      "the message composer is ENABLED while src/crypto/signal.ts still throws.",
      "Sending would put PLAINTEXT on the wire under a UI carrying a lock icon. " +
        "Either land the libsignal pipeline or restore editable={false}.",
    );
  }
  if (!/accessibilityState=\{\{ disabled: true \}\}/.test(stripComments(threadSource))) {
    fail(
      "the send control no longer reports itself disabled while the crypto is a stub.",
      "A control that announces itself as enabled to VoiceOver is the same bug class " +
        "c228 fixed on the comment button.",
    );
  }

  // -------------------------------------------------------------------------
  // 3. Stubbed crypto: every encryption claim must be on the allowlist.
  // -------------------------------------------------------------------------
  for (const [label, source] of [["messages/index.tsx", listSource], ["messages/[id].tsx", threadSource]]) {
    for (const text of userVisibleStrings(source)) {
      if (!MENTIONS_CRYPTO.test(text)) continue;
      if (ALLOWED_CLAIMS.has(text)) continue;
      fail(
        `${label} shows "${text}" while src/crypto/signal.ts still throws.`,
        "If it is genuinely true today, add it to ALLOWED_CLAIMS in this script " +
          "with the reason. If it is not, fix the copy.",
      );
    }
  }
} else {
  // -------------------------------------------------------------------------
  // Crypto is real now. Fail LOUDLY and on purpose: this script's whole premise
  // (and the deliberately cautious copy it protects) was written for the stub,
  // and the copy must be re-reviewed rather than left under-claiming forever.
  // -------------------------------------------------------------------------
  fail(
    "src/crypto/signal.ts no longer looks like a stub - the client crypto has landed.",
    `${todoThrows.length} of ${exportedFns.length} exported functions still throw. ` +
      "This is not a code defect: it means the Messages copy must be re-reviewed now " +
      "that an encryption claim could finally be TRUE, and this guard updated to match. " +
      "See board c254.",
  );
}

if (failures.length > 0) {
  console.error("FAIL  verify:e2ee-claims (board c254)\n");
  for (const f of failures) console.error(`  - ${f}\n`);
  process.exit(1);
}

console.log(
  `OK  verify:e2ee-claims - client crypto is still a stub (${todoThrows.length}/${exportedFns.length} ` +
    "functions throw), the composer is disabled, and every encryption string in the Messages " +
    `screens is on the allowlist (${ALLOWED_CLAIMS.size}).`,
);
