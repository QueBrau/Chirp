/**
 * Guards the app's encryption copy against the state of the client crypto
 * (board cards c254, rewritten for c451).
 *
 *   npm run verify:e2ee-claims            # the guard
 *   node scripts/verify-e2ee-claims.mjs --self-test   # proves the guard can fail
 *
 * WHY THIS EXISTS. c254: the Messages screens shipped "End-to-end encrypted" while
 * src/crypto/signal.ts was a stub whose every function throws, with zero callers. The
 * app told users a security property it did not have, the same family as c76's /terms
 * claiming powers the server lacked.
 *
 * WHAT CHANGED IN c451 (E2EE milestone M4). Direct messages are now REALLY encrypted on
 * iPhone: src/crypto/ drives the native core (vodozemac Olm in Rust, modules/chirp-crypto),
 * and src/messages/DmThread.tsx sends and reads real ciphertext. So the old rule ("the
 * crypto is a stub, therefore no claim and no composer") is no longer the truth and this
 * guard was rewritten to say the new truth: an encryption claim is allowed in EXACTLY the
 * one place whose code path is real, and nowhere else, in both directions:
 *
 *   - A claim reappears somewhere the crypto does not back (groups, web, Android, a
 *     screen that does not know this phone's state, a phone still waiting for approval).
 *   - A composer is enabled without the real path behind it.
 *   - The "real path" quietly stops being real (a stub, a plaintext send, a platform the
 *     module does not cover) while the claim stays.
 *
 * It is no weaker than before: every old check still runs where it still applies (groups),
 * the scan now covers every screen and component rather than only the Messages folder, and
 * new checks pin the real path. No jest/vitest exists in this repo (see the sibling
 * verify-*.mjs scripts), so this reads the real source files rather than importing them.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const read = (rel) => readFileSync(join(ROOT, rel), "utf8");
const exists = (rel) => existsSync(join(ROOT, rel));

const MENTIONS_CRYPTO = /encrypt|e2ee|end.to.end/i;

/** Every user-visible encryption string, per file, with the reason it is true there. */
const ALLOWED = {
  // Groups still have no crypto at all. This is the one pre-c451 string left, kept only for
  // the group branch of the thread screen (checked to be group-gated below). The group
  // composer's note used to be "Sending unlocks with E2EE (milestone 4)"; it is now the
  // plain, claim-free GROUP_COPY below, and the stale wording is banned everywhere.
  "app/(tabs)/messages/[id].tsx": new Map([
    [
      "Encrypted message",
      "Placeholder bubble for a GROUP message body this client cannot decrypt. TRUE: the " +
        "server genuinely stores opaque ciphertext (SPEC 6.2/6.5); the gap is client-side group crypto.",
    ],
  ]),
};

/** What the group thread says instead: true today, and it claims nothing. */
const GROUP_COPY = "Group messages aren't available yet.";
/** The DM thread's copy for a server that has no v2 API yet. No claim, no error. */
const COMING_SOON_COPY = "Direct messages are coming soon.";

/** The ONE place a present-tense claim may live: the ready branch of the DM thread. */
const DM_FILE = "src/messages/DmThread.tsx";
const DM_READY_FN = "ReadyThread";
const DM_NOT_READY_FN = "NotReadyThread";
const DM_CLAIMS = new Map([
  [
    "End-to-end encrypted",
    "Shown by ReadyThread only, which DmThread renders only when the engine reports " +
      "status === \"ready\": this phone's device is registered, approved, and backed by the " +
      "native Olm core (iOS). Direct messages only.",
  ],
]);

// ---------------------------------------------------------------------------
// Source helpers
// ---------------------------------------------------------------------------

/** Strip comments so prose ABOUT encryption is never mistaken for a UI claim. */
function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/** Import and re-export specifiers are paths, not copy ("@/crypto/e2eeRuntime"). */
function stripModuleSpecifiers(source) {
  return source
    .replace(/^\s*import\s[\s\S]*?\sfrom\s+["'][^"']*["'];?\s*$/gm, "")
    .replace(/^\s*import\s+["'][^"']*["'];?\s*$/gm, "")
    .replace(/^\s*export\s[\s\S]*?\sfrom\s+["'][^"']*["'];?\s*$/gm, "");
}

/**
 * Quoted string literals plus JSX text nodes - everything a user can read.
 *
 * Literals are lexed, not regex-matched: a JSX text node or a string such as "isn't" holds
 * an apostrophe, and a naive /'...'/ pairs it with the NEXT apostrophe and reads half the
 * file as one "string". So '"' and "'" literals end at their closing quote or at the end of
 * the line (whichever comes first), and an unterminated one is dropped.
 */
function userVisibleStrings(source) {
  const body = stripModuleSpecifiers(stripComments(source));
  const found = new Set();
  for (let i = 0; i < body.length; i++) {
    const quote = body[i];
    if (quote !== '"' && quote !== "'" && quote !== "`") continue;
    let j = i + 1;
    let closed = false;
    for (; j < body.length; j++) {
      const c = body[j];
      if (c === "\\") { j++; continue; }
      if (c === quote) { closed = true; break; }
      if (c === "\n" && quote !== "`") break;
    }
    if (closed) {
      found.add(body.slice(i + 1, j));
      i = j;
    }
  }
  for (const m of body.matchAll(/>([^<>{}]+)</g)) {
    const text = m[1].replace(/\s+/g, " ").trim();
    if (text) found.add(text);
  }
  return [...found];
}

/**
 * The source of `function name(...) { ... }` or a class method `[private] [async] name(...) { ... }`,
 * or null. Balances parens, then braces.
 */
function functionSource(source, name) {
  let start = source.search(new RegExp(`function\\s+${name}\\s*\\(`));
  if (start === -1) start = source.search(new RegExp(`^[ \\t]*(?:private\\s+|public\\s+)?(?:async\\s+)?${name}\\s*\\(`, "m"));
  if (start === -1) return null;
  let i = source.indexOf("(", start);
  for (let depth = 0; i < source.length; i++) {
    if (source[i] === "(") depth++;
    else if (source[i] === ")" && --depth === 0) break;
  }
  // A return type written as an object literal (`): { text: string } {`) has braces of its
  // own; skip it so the body is found, not the annotation.
  const annotation = /^\s*:\s*\{/.exec(source.slice(i + 1));
  let from = i;
  if (annotation) {
    let typeOpen = i + 1 + annotation[0].length - 1;
    let typeDepth = 0;
    for (; typeOpen < source.length; typeOpen++) {
      if (source[typeOpen] === "{") typeDepth++;
      else if (source[typeOpen] === "}" && --typeDepth === 0) break;
    }
    from = typeOpen;
  }
  const open = source.indexOf("{", from + 1);
  if (open === -1) return null;
  let depth = 0;
  for (let j = open; j < source.length; j++) {
    if (source[j] === "{") depth++;
    else if (source[j] === "}" && --depth === 0) return source.slice(start, j + 1);
  }
  return null;
}

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(join(ROOT, dir))) {
    const rel = `${dir}/${entry}`;
    if (statSync(join(ROOT, rel)).isDirectory()) out.push(...walk(rel));
    else out.push(rel);
  }
  return out;
}

/**
 * Every source file a message body passes through between the composer and the native store
 * (and back): the JS engine and screens, the Swift module, the Rust core. Keyed by path.
 */
function plaintextPathFiles() {
  const files = {};
  for (const rel of walk("src/crypto")) {
    if (/^src\/crypto\/(e2ee[A-Za-z]*\.ts|E2eeProvider\.tsx|native\.ts)$/.test(rel)) files[rel] = read(rel);
  }
  for (const rel of walk("src/messages")) if (rel.endsWith(".tsx")) files[rel] = read(rel);
  files["src/api/e2ee.ts"] = read("src/api/e2ee.ts");
  for (const rel of walk("modules/chirp-crypto/ios")) {
    if (rel.endsWith(".swift") && !rel.includes("/Generated/")) files[rel] = read(rel);
  }
  for (const rel of walk("modules/chirp-crypto/rust/src")) {
    if (rel.endsWith(".rs") && !rel.includes("/tests/")) files[rel] = read(rel);
  }
  return files;
}

/** Everything the checks read, so the self-test can hand the same checks mutated copies. */
function loadSources() {
  const ui = {};
  for (const dir of ["app", "src"]) {
    for (const rel of walk(dir)) {
      // .tsx is UI. src/crypto/*.ts is logic whose strings are error codes, checked separately.
      if (rel.endsWith(".tsx")) ui[rel] = read(rel);
    }
  }
  const messagesDir = "app/(tabs)/messages";
  return {
    ui,
    messageScreens: readdirSync(join(ROOT, messagesDir)).filter((f) => f.endsWith(".tsx")).sort().map((f) => `${messagesDir}/${f}`),
    thread: ui[`${messagesDir}/[id].tsx`] ?? "",
    dm: ui[DM_FILE] ?? "",
    stubs: Object.fromEntries(["signal", "keys", "groups"].map((n) => [n, read(`src/crypto/${n}.ts`)])),
    moduleConfig: read("modules/chirp-crypto/expo-module.config.json"),
    androidDirExists: exists("modules/chirp-crypto/android"),
    swift: read("modules/chirp-crypto/ios/ChirpCryptoModule.swift"),
    rustCore: read("modules/chirp-crypto/rust/src/core.rs"),
    native: read("src/crypto/native.ts"),
    runtime: read("src/crypto/e2eeRuntime.ts"),
    engine: read("src/crypto/e2eeEngine.ts"),
    apiE2ee: read("src/api/e2ee.ts"),
    errors: read("src/crypto/e2eeErrors.ts"),
    plaintextPath: plaintextPathFiles(),
  };
}

// ---------------------------------------------------------------------------
// The checks. Each returns failure strings; none touches the disk.
// ---------------------------------------------------------------------------

function check(src) {
  const failures = [];
  const fail = (msg, detail) => failures.push(detail ? `${msg}\n      ${detail}` : msg);
  const code = (s) => stripComments(s);

  // --- 0. Anchors: a directory move must not turn the scans into vacuous passes. -------
  for (const anchor of ["index.tsx", "[id].tsx", "new.tsx"]) {
    if (!src.messageScreens.some((p) => p.endsWith(`/${anchor}`))) {
      fail(`app/(tabs)/messages/${anchor} not found`, "(screens moved? update this script's anchors before trusting it)");
    }
  }
  if (!src.dm) fail(`${DM_FILE} not found`, "(the DM thread moved? update this script's anchors before trusting it)");
  if (Object.keys(src.ui).length < 10) fail(`only ${Object.keys(src.ui).length} .tsx files scanned`, "(wrong directory?)");

  // --- 1. Group crypto (and the legacy Signal stubs) are still stubs. ------------------
  // Groups are the reason an encryption claim must stay out of every group path. If one of
  // these files stops being a stub the copy has to be re-reviewed, so this fails LOUDLY.
  let stubsAllThrow = true;
  for (const [name, source] of Object.entries(src.stubs)) {
    const fns = [...source.matchAll(/export async function (\w+)/g)].map((m) => m[1]);
    const throwing = [...source.matchAll(/throw new Error\("TODO\(milestone-3\): libsignal"\)/g)];
    if (fns.length === 0) fail(`no exported functions found in src/crypto/${name}.ts`, "(restructured? update this script's anchors)");
    if (fns.length !== throwing.length) {
      stubsAllThrow = false;
      fail(
        `src/crypto/${name}.ts no longer looks like a stub (${throwing.length} of ${fns.length} exported functions still throw).`,
        "This is not a code defect: it means group or legacy crypto may have landed, so the Messages " +
          "copy must be re-reviewed now that a claim could be TRUE there, and this guard updated to match.",
      );
    }
  }

  // --- 2. The real DM path is real. -----------------------------------------------------
  let config;
  try {
    config = JSON.parse(src.moduleConfig);
  } catch {
    fail("modules/chirp-crypto/expo-module.config.json is not valid JSON");
  }
  if (config) {
    const platforms = JSON.stringify(config.platforms);
    if (platforms !== '["ios"]') {
      fail(
        `the native crypto module's platforms are ${platforms}, not ["ios"].`,
        "Web and Android must report encryption as unavailable. If an Android adapter has landed, " +
          "extend this guard (and the copy in DmThread's notReadyCopy) in the same change; do not just edit the list.",
      );
    }
  }
  if (src.androidDirExists) fail("modules/chirp-crypto/android exists", "An Android adapter is a platform this guard does not cover yet.");
  for (const fn of ["encrypt", "decrypt"]) {
    if (!new RegExp(`AsyncFunction\\("${fn}"\\)`).test(src.swift)) {
      fail(`the Swift module no longer exposes "${fn}"`, "The DM path calls it; without it the claim is not backed.");
    }
  }
  if (!/create_outbound_session/.test(src.rustCore) || !/session\s*\.encrypt\(/.test(src.rustCore)) {
    fail("rust/src/core.rs no longer performs Olm encryption (create_outbound_session / session.encrypt).");
  }
  if (!/requireOptionalNativeModule/.test(code(src.native)) || !/export function isE2EEAvailable/.test(code(src.native))) {
    fail("src/crypto/native.ts no longer gates on the native module being present (isE2EEAvailable).");
  }
  if (!/available:\s*isE2EEAvailable/.test(code(src.runtime)) || !/crypto:\s*chirpCrypto/.test(code(src.runtime))) {
    fail("src/crypto/e2eeRuntime.ts does not wire the engine to chirpCrypto and isE2EEAvailable.");
  }
  const engine = code(src.engine);
  if (!/if\s*\(\s*!this\.ports\.available\(\)\s*\)\s*\{\s*return this\.setState\(\{\s*status:\s*"unavailable"/.test(engine)) {
    fail("the engine no longer returns status \"unavailable\" before touching storage or the network when the module is absent.");
  }
  const attempt = functionSource(engine, "attempt");
  const encryptFor = functionSource(engine, "encryptFor");
  if (attempt === null || encryptFor === null) {
    fail("the engine's attempt/encryptFor methods were not found", "(restructured? update this script's anchors)");
  } else {
    const encryptAt = attempt.search(/this\.encryptFor\(/);
    const postAt = attempt.search(/api\.sendMessageV2\(/);
    if (encryptAt === -1 || postAt === -1 || encryptAt > postAt) {
      fail("the engine must encrypt (encryptFor) before it posts (api.sendMessageV2).", "Sending plaintext under a lock icon is the bug this guard exists for.");
    }
    if (!/crypto\.encrypt\(/.test(encryptFor)) {
      fail("encryptFor no longer calls the native core (crypto.encrypt).");
    }
    if (postAt !== -1 && !/legs:\s*legs\.map\(/.test(attempt.slice(postAt, postAt + 400))) {
      fail("the engine's POST no longer sends the legs the core returned.");
    }
  }
  // The v2 send body takes legs, never text. A `body`/`text`/`plaintext` field here would be a plaintext path.
  const sendBody = /export interface SendMessageV2Body\s*\{([\s\S]*?)\}/.exec(code(src.apiE2ee));
  if (!sendBody) fail("src/api/e2ee.ts: SendMessageV2Body not found");
  else if (/\b(body|text|plaintext|content|message)\s*[?:]/.test(sendBody[1])) {
    fail("SendMessageV2Body has a text-like field.", "The server must only ever receive ciphertext legs.");
  }
  // No screen or component may use the v1 sender (it posts a caller-supplied blob as-is).
  for (const [rel, source] of Object.entries(src.ui)) {
    if (/\bsendMessage\b/.test(stripComments(source)) && /from\s+["']@\/api\/messages["']/.test(source)) {
      fail(`${rel} uses the v1 sendMessage`, "The v1 endpoint stores whatever blob it is given; the DM path must go through the engine.");
    }
  }

  // --- 3. Claims. ----------------------------------------------------------------------
  // 3a. Every user-visible encryption string, in every .tsx, must be on a list for its file.
  for (const [rel, source] of Object.entries(src.ui)) {
    if (rel === DM_FILE) continue; // Handled by 3b, which is stricter.
    const allowed = ALLOWED[rel] ?? new Map();
    for (const text of userVisibleStrings(source)) {
      if (!MENTIONS_CRYPTO.test(text)) continue;
      if (allowed.has(text)) continue;
      fail(
        `${rel} shows "${text}", an encryption claim nothing here backs.`,
        "Only the ready branch of the DM thread may claim encryption. If this is genuinely true here, " +
          "add it to ALLOWED in this script with the reason; if not, fix the copy.",
      );
    }
  }

  // 3b. In the DM file, a claim may appear ONLY inside ReadyThread.
  const ready = functionSource(src.dm, DM_READY_FN);
  const notReady = functionSource(src.dm, DM_NOT_READY_FN);
  if (ready === null) fail(`${DM_FILE}: function ${DM_READY_FN} not found`);
  if (notReady === null) fail(`${DM_FILE}: function ${DM_NOT_READY_FN} not found`);
  // Inside ReadyThread only DM_CLAIMS may appear; anywhere else in the file nothing may.
  const outsideReady = ready === null ? src.dm : src.dm.replace(ready, "");
  for (const text of userVisibleStrings(outsideReady)) {
    if (MENTIONS_CRYPTO.test(text)) {
      fail(`${DM_FILE} shows "${text}" outside ${DM_READY_FN}.`, "The claim must live where the ready check guards it.");
    }
  }
  for (const text of ready === null ? [] : userVisibleStrings(ready)) {
    if (MENTIONS_CRYPTO.test(text) && !DM_CLAIMS.has(text)) fail(`${DM_FILE} shows "${text}", which is not on DM_CLAIMS.`);
  }
  if (notReady !== null) {
    if (userVisibleStrings(notReady).some((t) => MENTIONS_CRYPTO.test(t))) {
      fail(`${DM_FILE}: ${DM_NOT_READY_FN} mentions encryption`, "A phone that is not ready must not claim anything.");
    }
    if (!/<Composer[\s\S]*?enabled=\{false\}/.test(notReady)) {
      fail(`${DM_FILE}: ${DM_NOT_READY_FN} no longer renders a DISABLED composer.`, "Sending without a ready device would have nothing to encrypt with.");
    }
  }

  // 3c. ReadyThread is reachable only through the ready check, from exactly one place.
  const dmFn = functionSource(code(src.dm), "DmThread");
  if (dmFn === null) fail(`${DM_FILE}: function DmThread not found`);
  else {
    const gate = dmFn.search(/status\s*===\s*"ready"/);
    const render = dmFn.search(/<ReadyThread\b/);
    if (gate === -1 || render === -1 || gate > render) {
      fail("DmThread renders ReadyThread without checking status === \"ready\" first.");
    }
  }
  if ((code(src.dm).match(/<ReadyThread\b/g) ?? []).length !== 1) {
    fail("ReadyThread must be rendered from exactly one place (DmThread, behind the ready check).");
  }
  if (ready !== null && !/enabled=\{canSend\}/.test(ready)) {
    fail("ReadyThread's composer is not driven by canSend (peer present and no hard stop).");
  }
  if (ready !== null && !/const canSend\s*=\s*peerUserId\s*!==\s*null\s*&&\s*block\s*===\s*null/.test(ready)) {
    fail("canSend no longer requires a peer and no trust hard stop.");
  }

  // 3d. The thread screen shows the DM component only for DMs, and keeps groups disabled.
  const thread = code(src.thread);
  if (!/const isDm\s*=\s*conversation\?\.kind\s*===\s*"dm"/.test(thread)) {
    fail("[id].tsx: isDm is no longer derived from conversation?.kind === \"dm\".");
  }
  const dmRender = thread.search(/<DmThread\b/);
  if (dmRender === -1 || !/isDm\s*&&\s*!denied\s*\?\s*\(\s*<DmThread\b/.test(thread)) {
    fail("[id].tsx: <DmThread> is not rendered behind isDm.");
  }
  const groupGate = thread.search(/conversation\?\.kind\s*===\s*"group"\s*\?\s*\(\s*<View/);
  const disabledComposerAt = thread.search(/editable=\{false\}/);
  if (groupGate === -1 || disabledComposerAt === -1 || groupGate > disabledComposerAt) {
    fail("[id].tsx: the disabled group composer is no longer behind kind === \"group\".", "Groups have no crypto: they keep a composer that cannot send.");
  }
  if (!/accessibilityState=\{\{ disabled: true \}\}/.test(thread)) {
    fail("[id].tsx: the group send control no longer reports itself disabled.", "A control that announces itself as enabled to VoiceOver is the c228 bug class.");
  }
  // The group composer says GROUP_COPY (text and accessibility hint), only inside the group branch.
  const composerNote = thread.indexOf(GROUP_COPY);
  if (composerNote === -1 || groupGate === -1 || composerNote < groupGate) {
    fail(`[id].tsx: the group composer no longer says "${GROUP_COPY}" inside the group branch.`);
  }
  if ((thread.match(new RegExp(GROUP_COPY.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")) ?? []).length < 2) {
    fail("[id].tsx: the group send control's accessibility hint no longer matches its visible note.", "c336: the hint reuses the visible wording.");
  }
  // Internal milestone jargon is not copy. It went stale the day M4 landed.
  for (const [rel, source] of Object.entries(src.ui)) {
    for (const text of userVisibleStrings(source)) {
      if (/milestone/i.test(text)) fail(`${rel} shows "${text}": internal milestone wording is not user copy.`);
    }
  }
  const placeholderAt = thread.indexOf("Encrypted message");
  const groupMapAt = thread.search(/conversation\?\.kind\s*===\s*"group"\s*\?\s*messages\.map/);
  if (placeholderAt !== -1 && (groupMapAt === -1 || groupMapAt > placeholderAt)) {
    fail("[id].tsx shows the \"Encrypted message\" placeholder outside the group branch.");
  }

  // --- 4. A server without the v2 API is "coming soon", not an error. -------------------
  // Production is not redeployed with /v2 when this ships. The engine must probe BEFORE it
  // opens or creates anything, map a 404/405 to unavailable/server_not_ready (no diagnostics),
  // and the DM thread must say COMING_SOON_COPY for it without a warning or a retry.
  const bring = functionSource(engine, "bringUp");
  if (bring === null) fail("the engine's bringUp method was not found", "(restructured? update this script's anchors)");
  else {
    const probeAt = bring.search(/this\.probeServer\(/);
    const unlockAt = bring.search(/crypto\.unlock\(/);
    if (probeAt === -1 || unlockAt === -1 || probeAt > unlockAt) {
      fail("the engine must probe the server (probeServer) before it opens or creates anything (crypto.unlock).",
        "A build pointed at a server without /v2 must not create, register or wipe a device.");
    }
  }
  const failStart = functionSource(engine, "failStart");
  if (failStart === null || !/isServerWithoutV2\(error\)\)\s*return this\.setState\(\{\s*status:\s*"unavailable",\s*reason:\s*SERVER_NOT_READY/.test(failStart)) {
    fail("the engine no longer maps a 404/405 from the v2 API to unavailable / server_not_ready.");
  }
  if (failStart !== null && /this\.report\(/.test(failStart.slice(0, failStart.indexOf("isServerWithoutV2")))) {
    fail("a server without v2 must not log a diagnostics line.");
  }
  // c436: while an account deletion runs the server answers every v2 write with 423. That is a
  // clear, non-retrying state with true copy, never an "error, try again".
  if (failStart === null || !/isAccountDeleting\(error\)\)\s*return this\.setState\(\{\s*status:\s*"unavailable",\s*reason:\s*ACCOUNT_DELETING/.test(failStart)) {
    fail("the engine no longer maps 423 account_deletion_in_progress to unavailable / account_deletion_in_progress.");
  }
  if (!/export function isAccountDeleting[\s\S]*?status === 423 && e\.detail === "account_deletion_in_progress"/.test(src.errors)) {
    fail("isAccountDeleting no longer means a 423 account_deletion_in_progress.");
  }
  if (!/if \(isAccountDeleting\(error\)\) return refuse\(new E2eeSendError\("account_deletion"\)\)/.test(engine)) {
    fail("a send refused with 423 must become the typed account_deletion failure (not a retry).");
  }
  if (!/if \(reason === ACCOUNT_DELETING\) return \{ text: "[^"]+", action: null \}/.test(code(src.dm))) {
    fail("DmThread's notReadyCopy has no retry-free copy for an account deletion.");
  }
  if (!/export function isServerWithoutV2[\s\S]*?status === 404 \|\| e\.status === 405/.test(src.errors)) {
    fail("isServerWithoutV2 no longer means a 404 or 405.");
  }
  const dmSource = code(src.dm);
  const copyFn = functionSource(dmSource, "notReadyCopy");
  if (copyFn === null || !copyFn.includes(COMING_SOON_COPY) || !/reason === SERVER_NOT_READY/.test(copyFn)) {
    fail(`DmThread's notReadyCopy no longer answers server_not_ready with "${COMING_SOON_COPY}".`);
  }
  if (notReady !== null) {
    const notReadyCode = code(notReady);
    if (!/quiet\s*\?\s*\(\s*<Notice\b/.test(notReadyCode) || !/reason\s*===\s*SERVER_NOT_READY\s*\|\|\s*reason\s*===\s*ACCOUNT_DELETING/.test(notReadyCode)) {
      fail("NotReadyThread no longer shows a quiet Notice (not a warning Banner) for server_not_ready and an account deletion.");
    }
    if (!/__DEV__\s*&&\s*reason\s*!==\s*null\s*&&\s*!quiet/.test(notReadyCode)) {
      fail("NotReadyThread would show a dev failure code on a quiet state.");
    }
  }

  // --- 6. Nothing on the plaintext path logs. -------------------------------------------
  // Production serves /v2, so the next build does real E2EE against it. A body, a key or an
  // id in a log line (JS console, os_log, a Rust println) would leak what the design keeps
  // off every server and every log. The ONE permitted line is the dev-only diagnostics
  // print, which carries a place name and a fixed code and nothing else.
  const REPORT_LINE = /if \(__DEV__\) console\.warn\(`\[e2ee\] \$\{where\}: \$\{code\}`\);/;
  for (const [rel, source] of Object.entries(src.plaintextPath)) {
    let body = stripComments(source);
    if (rel === "src/crypto/e2eeRuntime.ts") body = body.replace(REPORT_LINE, "");
    const bad = rel.endsWith(".swift")
      ? /\b(print|debugPrint|NSLog|os_log|dump)\s*\(|\bLogger\s*\(/.exec(body)
      : rel.endsWith(".rs")
        ? /\b(println|eprintln|print|eprint|dbg)!|\b(log|tracing)::/.exec(body)
        : /\bconsole\s*\.|\bAlert\s*\.|\balert\s*\(|\bshowApiError\b/.exec(body);
    if (bad) fail(`${rel} logs or alerts (${bad[0].trim()})`, "Nothing on the plaintext path may log; the one allowed line is the dev-only diagnostics print in e2eeRuntime.ts.");
  }
  if (!REPORT_LINE.test(stripComments(src.runtime))) {
    fail("the dev diagnostics line in e2eeRuntime.ts changed shape.", "It must print only a place name and a fixed code.");
  }
  if (!/safeCode\(where\)[\s\S]*?safeCode\(/.test(engine)) {
    fail("the engine's report() no longer restricts diagnostics to fixed, word-shaped codes (safeCode).");
  }

  // --- 5. The DM thread opens at the newest message. ------------------------------------
  // An inverted list inside a Screen that does not scroll, with the composer pinned under it.
  if (!/<FlatList[\s\S]*?\binverted\b/.test(code(ready ?? ""))) {
    fail("ReadyThread's message list is no longer an inverted FlatList (the thread must open at the newest message).");
  }
  if (!/<Screen\s+[^>]*scroll=\{!isDm\}/.test(code(src.thread).replace(/\/\/[^\n]*/g, ""))) {
    fail("[id].tsx: a DM thread must not live inside Screen's ScrollView (scroll={!isDm}).");
  }

  return { failures, stubsAllThrow };
}

// ---------------------------------------------------------------------------
// Main and self-test
// ---------------------------------------------------------------------------

const MUTATIONS = [
  {
    name: "a claim moved out of ReadyThread",
    apply: (s) => ({ ...s, dm: s.dm.replace("Getting this phone ready…", "End-to-end encrypted") }),
  },
  {
    name: "ReadyThread rendered without the ready check",
    apply: (s) => ({ ...s, dm: s.dm.replace(/if \(state\.status === "ready"\) return <ReadyThread/, "if (true) return <ReadyThread") }),
  },
  {
    name: "a claim added to the inbox screen",
    apply: (s) => ({ ...s, ui: { ...s.ui, "app/(tabs)/messages/index.tsx": s.ui["app/(tabs)/messages/index.tsx"] + '\nconst x = <Text>Your messages are end-to-end encrypted</Text>;' } }),
  },
  {
    name: "a claim added to a screen outside the Messages folder",
    apply: (s) => ({ ...s, ui: { ...s.ui, "app/(tabs)/profile/index.tsx": (s.ui["app/(tabs)/profile/index.tsx"] ?? "") + '\nconst y = "Encrypted backups";' } }),
  },
  {
    name: "the group composer enabled",
    apply: (s) => ({ ...s, thread: s.thread.replace("editable={false}", "editable={true}"), ui: { ...s.ui, "app/(tabs)/messages/[id].tsx": s.thread.replace("editable={false}", "editable={true}") } }),
  },
  {
    name: "the group composer shown to DMs",
    apply: (s) => ({ ...s, thread: s.thread.replace(/conversation\?\.kind === "group" \? \(\s*<View style=\{\{ marginTop/, "true ? (\n<View style={{ marginTop") }),
  },
  {
    name: "the native module declares Android",
    apply: (s) => ({ ...s, moduleConfig: s.moduleConfig.replace('["ios"]', '["ios", "android"]') }),
  },
  {
    name: "the engine posts without encrypting",
    apply: (s) => ({ ...s, engine: s.engine.replace("const legs = await this.encryptFor(session, directories, entry, body);", "const legs: Leg[] = [];") }),
  },
  {
    name: "a plaintext field on the v2 send body",
    apply: (s) => ({ ...s, apiE2ee: s.apiE2ee.replace("envelope_version: number;\n  legs: LegBody[];", "envelope_version: number;\n  body: string;\n  legs: LegBody[];") }),
  },
  {
    name: "group crypto stops being a stub",
    apply: (s) => ({ ...s, stubs: { ...s.stubs, groups: s.stubs.groups.replace('throw new Error("TODO(milestone-3): libsignal")', "return") } }),
  },
  {
    name: "the engine stops treating a missing v2 API as coming soon",
    apply: (s) => ({ ...s, engine: s.engine.replace("if (isServerWithoutV2(error)) return this.setState({ status: \"unavailable\", reason: SERVER_NOT_READY });", "") }),
  },
  {
    name: "the engine opens the store before it probes the server",
    apply: (s) => ({ ...s, engine: s.engine.replace("await this.probeServer(userId);\n    let generation", "let generation").replace("let handle = await crypto.unlock(userId, generation);", "let handle = await crypto.unlock(userId, generation);\n    await this.probeServer(userId);") }),
  },
  {
    name: "an account deletion becomes an error with a retry button",
    apply: (s) => ({ ...s, engine: s.engine.replace(/if \(isAccountDeleting\(error\)\) return this\.setState\(\{ status: "unavailable", reason: ACCOUNT_DELETING \}\);/, "") }),
  },
  {
    name: "a send refused for an account deletion is retried",
    apply: (s) => ({ ...s, engine: s.engine.replace('if (isAccountDeleting(error)) return refuse(new E2eeSendError("account_deletion"));', "") }),
  },
  {
    name: "coming soon claims encryption",
    apply: (s) => ({ ...s, dm: s.dm.replace("Direct messages are coming soon.", "Encrypted direct messages are coming soon.") }),
  },
  {
    name: "coming soon is shown as a warning banner",
    apply: (s) => ({ ...s, dm: s.dm.replace("quiet ? (\n        <Notice text={text} />", "quiet ? (\n        <Banner text={text} />") }),
  },
  {
    name: "the group note goes back to milestone wording",
    apply: (s) => ({ ...s, thread: s.thread.replace("Group messages aren't available yet.", "Sending unlocks with E2EE (milestone 4)"), ui: { ...s.ui, "app/(tabs)/messages/[id].tsx": s.thread.replace("Group messages aren't available yet.", "Sending unlocks with E2EE (milestone 4)") } }),
  },
  {
    name: "milestone wording on another screen",
    apply: (s) => ({ ...s, ui: { ...s.ui, "app/(tabs)/messages/new.tsx": s.ui["app/(tabs)/messages/new.tsx"].replace(/>\s*Group messages aren't available yet\.\s*</, ">Unlocks in milestone 4<") } }),
  },
  {
    name: "the DM thread goes back into the scrolling Screen",
    apply: (s) => ({ ...s, thread: s.thread.replace("scroll={!isDm}", "scroll={true}") }),
  },
  {
    name: "the engine logs a message body",
    apply: (s) => ({ ...s, plaintextPath: { ...s.plaintextPath, "src/crypto/e2eeEngine.ts": s.plaintextPath["src/crypto/e2eeEngine.ts"] + "\nconsole.log(body);" } }),
  },
  {
    name: "the Swift module prints a message",
    apply: (s) => ({ ...s, plaintextPath: { ...s.plaintextPath, "modules/chirp-crypto/ios/ChirpCryptoModule.swift": s.plaintextPath["modules/chirp-crypto/ios/ChirpCryptoModule.swift"] + "\nprint(message.body)" } }),
  },
  {
    name: "the Rust core prints a body",
    apply: (s) => ({ ...s, plaintextPath: { ...s.plaintextPath, "modules/chirp-crypto/rust/src/core.rs": s.plaintextPath["modules/chirp-crypto/rust/src/core.rs"] + "\nfn leak(b: &str) { println!(\"{b}\"); }" } }),
  },
  {
    name: "the diagnostics print grows a message",
    apply: (s) => ({ ...s, runtime: s.runtime.replace("[e2ee] ${where}: ${code}", "[e2ee] ${where}: ${code} ${String(where)}"), plaintextPath: { ...s.plaintextPath, "src/crypto/e2eeRuntime.ts": s.plaintextPath["src/crypto/e2eeRuntime.ts"].replace("[e2ee] ${where}: ${code}", "[e2ee] ${where}: ${code} ${String(where)}") } }),
  },
  {
    name: "the DM list stops being inverted",
    apply: (s) => ({ ...s, dm: s.dm.replace("        inverted\n", "") }),
  },
  {
    name: "the engine stops reporting unavailable",
    apply: (s) => ({ ...s, engine: s.engine.replace('status: "unavailable"', 'status: "ready"') }),
  },
];

const sources = loadSources();
const selfTest = process.argv.includes("--self-test");

if (selfTest) {
  const base = check(sources);
  let bad = 0;
  if (base.failures.length > 0) {
    console.error("FAIL  self-test: the unmodified tree must pass first.\n");
    for (const f of base.failures) console.error(`  - ${f}\n`);
    process.exit(1);
  }
  for (const mutation of MUTATIONS) {
    const mutated = mutation.apply(sources);
    if (JSON.stringify(mutated) === JSON.stringify(sources)) {
      console.error(`  FAIL  mutation "${mutation.name}" changed nothing (anchor text moved?)`);
      bad++;
      continue;
    }
    const { failures } = check(mutated);
    if (failures.length === 0) {
      console.error(`  FAIL  the guard did NOT catch: ${mutation.name}`);
      bad++;
    } else {
      console.log(`  PASS  caught: ${mutation.name}`);
    }
  }
  console.log(bad === 0 ? `\nOK  verify:e2ee-claims self-test - ${MUTATIONS.length} mutations all caught.` : `\n${bad} FAILURE(S)`);
  process.exit(bad === 0 ? 0 : 1);
}

const { failures, stubsAllThrow } = check(sources);
if (failures.length > 0) {
  console.error("FAIL  verify:e2ee-claims (boards c254, c451)\n");
  for (const f of failures) console.error(`  - ${f}\n`);
  process.exit(1);
}

const scanned = Object.keys(sources.ui).length;
console.log(
  `OK  verify:e2ee-claims - the DM path is real (native Olm core, iOS only; the engine encrypts before it ` +
    `posts legs and reports "unavailable" without the module), the one DM claim lives in ${DM_READY_FN} behind the ` +
    `ready check, group/legacy crypto is still a stub (${stubsAllThrow ? "all three files throw" : "?"}) so the group ` +
    `composer stays disabled and group-gated, and no encryption string appears in the other ${scanned - 1} screens and components.`,
);
