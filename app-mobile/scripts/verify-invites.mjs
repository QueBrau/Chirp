/**
 * Static contract check for the e-board invite hand-off (board c5).
 *
 * The native share sheet and QR renderer are UI integrations, so they cannot
 * be exercised in this repo's headless Node environment. This keeps the
 * important wiring from silently regressing: a minted invite must share the
 * public https hand-off URL and the QR must encode that exact same URL.
 */
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const chapterScreen = readFileSync(new URL("../app/(tabs)/chapter/index.tsx", import.meta.url), "utf8");
const inviteLink = readFileSync(new URL("../src/auth/inviteLink.ts", import.meta.url), "utf8");
const app = JSON.parse(readFileSync(new URL("../app.json", import.meta.url), "utf8")).expo;

// Execute the real helper: a URL substring in source does not establish which
// host the compiled default branch actually returns.
const compiled = ts.transpileModule(inviteLink, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
function links(env = {}) {
  const context = { exports: {}, process: { env } };
  runInNewContext(compiled, context);
  return context.exports;
}
const publicLinks = links();
assert.equal(publicLinks.inviteShareUrl("invite_123"), "https://chirpsocials.com/join-chapter?code=invite_123");
assert.equal(publicLinks.inviteShareUrl("a+b?&"), "https://chirpsocials.com/join-chapter?code=a%2Bb%3F%26");
assert.equal(publicLinks.inviteShareUrl(), "https://chirpsocials.com/join-chapter");
assert.equal(links({ EXPO_PUBLIC_WEB_URL: "http://localhost:5173/" }).inviteShareUrl("test"),
  "http://localhost:5173/join-chapter?code=test");
assert.deepEqual(app.ios.associatedDomains, ["applinks:chirpsocials.com"]);
assert.deepEqual(app.android.intentFilters, [{
  action: "VIEW", autoVerify: true,
  data: [{ scheme: "https", host: "chirpsocials.com", path: "/join-chapter" }],
  category: ["BROWSABLE", "DEFAULT"],
}]);
console.log("  PASS  executed invite host, encoding, override and exact native route contracts");

const checks = [
  ["native share-sheet API is imported", /import \{[^}]*Share[^}]*\} from "react-native"/s, chapterScreen],
  ["invite share action calls Share.share", /Share\.share\(\{[\s\S]*?message:\s*`Join our org on Chirp:[\s\S]*?url,/s, chapterScreen],
  ["QR renderer is present", /import QRCode from "react-native-qrcode-svg"/, chapterScreen],
  ["QR encodes the public invite URL", /<QRCode[\s\S]*?value=\{inviteShareUrl\(invite\.code\)\}/s, chapterScreen],
  ["shared URL uses the https hand-off helper", /inviteShareUrl\(invite\.code\)/, chapterScreen],
  ["custom-scheme links are not shared", (text) => !/Share\.share\([\s\S]*?chirp:\/\//s.test(text), chapterScreen],
  // Board c359: this list was a flat 200-code cap with no continuation, so a code
  // past that ceiling was neither visible nor revocable here at all.
  ["listInvites is called with a page-shaped options object", /listInvites\(chapterId,\s*\{/, chapterScreen],
  ["a hasOlder-or-equivalent continuation flag exists", /\bhasOlder\b/, chapterScreen],
  ["a load-older-codes handler and button label exist", /loadOlderCodes|Load older codes/, chapterScreen],
];

let failures = 0;
for (const [label, check, source] of checks) {
  const pass = typeof check === "function" ? check(source) : check.test(source);
  if (pass) console.log(`  PASS  ${label}`);
  else {
    failures++;
    console.log(`  FAIL  ${label}`);
  }
}

if (!/WEB_BASE_URL/.test(inviteLink) || !/\/join-chapter/.test(inviteLink)) {
  failures++;
  console.log("  FAIL  inviteLink.ts exports the public join-chapter URL");
} else {
  console.log("  PASS  inviteLink.ts exports the public join-chapter URL");
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
