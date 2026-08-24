/**
 * Static contract check for the e-board invite hand-off (board c5).
 *
 * The native share sheet and QR renderer are UI integrations, so they cannot
 * be exercised in this repo's headless Node environment. This keeps the
 * important wiring from silently regressing: a minted invite must share the
 * public https hand-off URL and the QR must encode that exact same URL.
 */
import { readFileSync } from "node:fs";

const chapterScreen = readFileSync(new URL("../app/(tabs)/chapter/index.tsx", import.meta.url), "utf8");
const inviteLink = readFileSync(new URL("../src/auth/inviteLink.ts", import.meta.url), "utf8");

const checks = [
  ["native share-sheet API is imported", /import \{[^}]*Share[^}]*\} from "react-native"/s, chapterScreen],
  ["invite share action calls Share.share", /Share\.share\(\{[\s\S]*?message:\s*`Join our org on Chirp:[\s\S]*?url,/s, chapterScreen],
  ["QR renderer is present", /import QRCode from "react-native-qrcode-svg"/, chapterScreen],
  ["QR encodes the public invite URL", /<QRCode[\s\S]*?value=\{inviteShareUrl\(invite\.code\)\}/s, chapterScreen],
  ["shared URL uses the https hand-off helper", /inviteShareUrl\(invite\.code\)/, chapterScreen],
  ["custom-scheme links are not shared", (text) => !/Share\.share\([\s\S]*?chirp:\/\//s.test(text), chapterScreen],
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
