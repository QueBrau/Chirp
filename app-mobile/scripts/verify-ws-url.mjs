/**
 * Verifies wsUrl()'s EXPO_PUBLIC_WS_URL override (board card c213) — the
 * client-side half of moving realtime to its own Cloud Run service (chirp-ws)
 * independently of EXPO_PUBLIC_API_URL, without a rebuild.
 *
 *   npm run verify:ws-url
 *
 * No jest/vitest exists in this repo (see the sibling verify-*.mjs scripts) and
 * plain node here can't run TypeScript, so — same approach as verify-contract.mjs
 * — the REAL wsUrl() source is extracted from client.ts at run time, not
 * hand-copied: hand-copying it would only prove a maintained-by-hand duplicate
 * stays in sync with itself, not that the actual shipped function behaves
 * correctly. The extracted text has its `: string` return annotation stripped
 * (the only non-runtime-JS part of the function) and is then genuinely
 * executed via `new Function`, with `process` and `API_BASE_URL` injected so
 * each case controls its own inputs.
 */
import { readFileSync } from "node:fs";

const CLIENT_TS = new URL("../src/api/client.ts", import.meta.url);
const source = readFileSync(CLIENT_TS, "utf8");

const FN_START = "export function wsUrl(): string {";
const startIdx = source.indexOf(FN_START);
if (startIdx === -1) {
  console.error("FAIL  could not find `export function wsUrl(): string {` in client.ts");
  console.error("      (signature changed? update this script's extraction anchor)");
  process.exit(1);
}
// Non-greedy up to the first "\n}" after the opener. Safe as long as wsUrl's
// body is a single statement with no nested top-level brace of its own — true
// today (one `return ... ?? ...;` line) and worth re-reading this comment
// before trusting the extraction if that ever changes.
const endIdx = source.indexOf("\n}", startIdx);
if (endIdx === -1) {
  console.error("FAIL  could not find wsUrl()'s closing brace");
  process.exit(1);
}
const rawFn = source.slice(startIdx, endIdx + 2);
const runnableFn = rawFn.replace("export function wsUrl(): string {", "function wsUrl() {");

if (!rawFn.includes("EXPO_PUBLIC_WS_URL")) {
  console.error("FAIL  extracted wsUrl() no longer references EXPO_PUBLIC_WS_URL");
  console.error(rawFn);
  process.exit(1);
}

/** Build and call the REAL extracted wsUrl() against injected process.env / API_BASE_URL. */
function callRealWsUrl({ wsUrlEnv, apiBaseUrl }) {
  const harness = new Function(
    "process",
    "API_BASE_URL",
    `${runnableFn}\nreturn wsUrl();`,
  );
  const env = {};
  if (wsUrlEnv !== undefined) env.EXPO_PUBLIC_WS_URL = wsUrlEnv;
  return harness({ env }, apiBaseUrl);
}

let failures = 0;
const check = (name, actual, expected) => {
  if (actual === expected) {
    console.log(`  PASS  ${name}`);
  } else {
    console.log(`  FAIL  ${name}`);
    console.log(`        expected ${JSON.stringify(expected)}`);
    console.log(`        got      ${JSON.stringify(actual)}`);
    failures++;
  }
};

// --- unset: byte-for-byte today's behavior, derived from API_BASE_URL ---

check(
  "unset + prod API_BASE_URL -> derived wss:// + /ws (today's real default)",
  callRealWsUrl({ wsUrlEnv: undefined, apiBaseUrl: "https://chirp-api-593616178468.us-central1.run.app" }),
  "wss://chirp-api-593616178468.us-central1.run.app/ws",
);

check(
  "unset + local http API_BASE_URL -> derived ws:// + /ws (dev override case)",
  callRealWsUrl({ wsUrlEnv: undefined, apiBaseUrl: "http://localhost:8000" }),
  "ws://localhost:8000/ws",
);

// --- set: override wins verbatim, API_BASE_URL is not consulted at all ---

check(
  "set -> returned verbatim, independent of API_BASE_URL (garbage base proves it's unused)",
  callRealWsUrl({
    wsUrlEnv: "wss://chirp-ws-593616178468.us-central1.run.app/ws",
    apiBaseUrl: "not-a-real-url-should-never-be-read",
  }),
  "wss://chirp-ws-593616178468.us-central1.run.app/ws",
);

// `??` (not `||`) means an explicit "" is honored rather than falling through —
// same precedent as EXPO_PUBLIC_API_URL's own `?? default` above it in this file.
check(
  "set to empty string -> honored as-is, same `??` precedent as EXPO_PUBLIC_API_URL",
  callRealWsUrl({ wsUrlEnv: "", apiBaseUrl: "http://localhost:8000" }),
  "",
);

// --- eas.json: the BUILD-TIME half, which is what c246 was actually about ---
//
// c213 shipped wsUrl()'s override and the cases above prove the function works.
// Nothing ever SET the variable, though: all four build profiles left it unset,
// so every build derived the socket URL from the api host and every socket
// landed on chirp-api. chirp-ws sat dark - 14 days of its request logs held not
// one 101 upgrade, only manual probes. A green function with unset config looks
// exactly like a working feature, which is why these cases check the config
// itself rather than only the code that reads it.

const EAS = JSON.parse(readFileSync(new URL("../eas.json", import.meta.url), "utf8"));
const CHIRP_WS = "wss://chirp-ws-593616178468.us-central1.run.app/ws";
const profileWsUrl = (name) => EAS.build?.[name]?.env?.EXPO_PUBLIC_WS_URL;

for (const profile of ["preview", "production"]) {
  check(`eas.json "${profile}" sets EXPO_PUBLIC_WS_URL to chirp-ws`, profileWsUrl(profile), CHIRP_WS);
  // Not redundant with the override case above: that one feeds in a hand-written
  // value, this one feeds in whatever eas.json ACTUALLY ships, so a typo in the
  // host fails here instead of at runtime on a real phone.
  check(
    `eas.json "${profile}" value survives the real wsUrl() verbatim`,
    callRealWsUrl({ wsUrlEnv: profileWsUrl(profile), apiBaseUrl: "not-a-real-url-should-never-be-read" }),
    CHIRP_WS,
  );
}

// Both development profiles are UNSET ON PURPOSE, and this asserts it stays that
// way. A developer pointing EXPO_PUBLIC_API_URL at localhost must get a localhost
// socket; pinning the prod socket here would pair a local backend with PROD
// realtime, and that split brain reads as a realtime bug rather than as
// misconfiguration. "Finishing the job" by adding these two is the mistake this
// case exists to catch. development-simulator inherits via `extends`, so leaving
// development unset covers both.
for (const profile of ["development", "development-simulator"]) {
  check(`eas.json "${profile}" deliberately leaves EXPO_PUBLIC_WS_URL unset`, profileWsUrl(profile), undefined);
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
