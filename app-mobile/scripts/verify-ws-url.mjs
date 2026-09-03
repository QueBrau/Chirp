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
// body contains no brace at column 0 of its own — true today (c272 made the body
// several single-line statements, none of them a block that opens a brace onto
// its own line) and worth re-reading this comment before trusting the extraction
// if that ever changes.
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

// c272: wsUrl() now closes over two module constants, so they have to reach the
// harness. Read them OUT OF client.ts for the same reason the function itself is
// extracted rather than hand-copied — a copy here would only prove this file
// agrees with itself. If either disappears or is renamed, this fails loudly
// instead of injecting undefined and quietly changing what the cases mean.
const constFromClient = (name) => {
  const match = source.match(new RegExp(`const ${name} = "([^"]+)"`));
  if (!match) {
    console.error(`FAIL  could not read ${name} out of client.ts`);
    process.exit(1);
  }
  return match[1];
};
const DEFAULT_API_BASE_URL = constFromClient("DEFAULT_API_BASE_URL");
const DEFAULT_WS_URL = constFromClient("DEFAULT_WS_URL");

/** Build and call the REAL extracted wsUrl() against injected process.env / API_BASE_URL. */
function callRealWsUrl({ wsUrlEnv, apiBaseUrl }) {
  const harness = new Function(
    "process",
    "API_BASE_URL",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_WS_URL",
    `${runnableFn}\nreturn wsUrl();`,
  );
  const env = {};
  if (wsUrlEnv !== undefined) env.EXPO_PUBLIC_WS_URL = wsUrlEnv;
  return harness({ env }, apiBaseUrl, DEFAULT_API_BASE_URL, DEFAULT_WS_URL);
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

// THIS CASE USED TO ASSERT THE BUG (c272). It expected the default api host to
// derive wss://chirp-api/ws and called that "today's real default" - which it was,
// and which was exactly the defect: chirp-api is the API service, realtime lives on
// chirp-ws. A cloud development build sets no api url, so it fell through to here
// and put every socket on the wrong service, silently, because both services run
// the same image. Changed deliberately, not relaxed: the expectation is now the
// paired socket.
check(
  "unset + DEFAULT api host -> the paired chirp-ws socket, NOT scheme-swapped chirp-api (c272)",
  callRealWsUrl({ wsUrlEnv: undefined, apiBaseUrl: DEFAULT_API_BASE_URL }),
  DEFAULT_WS_URL,
);

// The invariant the development profiles exist to protect, and the reason the fix
// went in wsUrl() rather than into eas.json: a local api must still yield a LOCAL
// socket. Pinning EXPO_PUBLIC_WS_URL on the dev profiles would have fixed the cloud
// build by breaking this, pairing a local backend with PROD realtime.
check(
  "unset + local http API_BASE_URL -> derived ws:// + /ws (local dev stays local)",
  callRealWsUrl({ wsUrlEnv: undefined, apiBaseUrl: "http://localhost:8000" }),
  "ws://localhost:8000/ws",
);

check(
  "unset + a custom non-default https host -> still derived, not forced to chirp-ws",
  callRealWsUrl({ wsUrlEnv: undefined, apiBaseUrl: "https://staging.example.com" }),
  "wss://staging.example.com/ws",
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
//
// c272 CORRECTED THE REST OF THIS COMMENT, which was the most convincing wrong
// thing in the file. It argued the profiles are unset because the derivation
// handles them - true for `expo start` against localhost, FALSE for a cloud
// development build, which sets no api url at all, falls back to the prod api and
// used to derive a socket on chirp-api. Leaving these unset is still right; it is
// right because wsUrl() now pairs the default api with the default socket, not
// because scheme-swapping was ever sufficient on its own.
for (const profile of ["development", "development-simulator"]) {
  check(`eas.json "${profile}" deliberately leaves EXPO_PUBLIC_WS_URL unset`, profileWsUrl(profile), undefined);
}

// The drift guard c272 adds: client.ts's fallback socket and the value eas.json
// ships must be the SAME string. They are two independent copies of one host, and
// nothing else would notice them diverging - a build would simply start using a
// socket url that no profile agrees with, which is how this family of bug keeps
// arriving in the first place.
for (const profile of ["preview", "production"]) {
  check(
    `client.ts DEFAULT_WS_URL matches eas.json "${profile}" - no silent drift`,
    DEFAULT_WS_URL,
    profileWsUrl(profile),
  );
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
