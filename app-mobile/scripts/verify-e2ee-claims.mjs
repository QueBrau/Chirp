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
  // Groups still have no crypto at all. These two are the pre-c451 strings, kept only for
  // the group branch of the thread screen (checked to be group-gated below).
  "app/(tabs)/messages/[id].tsx": new Map([
    [
      "Encrypted message",
      "Placeholder bubble for a GROUP message body this client cannot decrypt. TRUE: the " +
        "server genuinely stores opaque ciphertext (SPEC 6.2/6.5); the gap is client-side group crypto.",
    ],
    [
      "Sending unlocks with E2EE (milestone 4)",
      "Forward-looking and explicitly not present tense: it tells the user group sending " +
        "is unavailable and why. Only reachable for kind === \"group\" (checked).",
    ],
  ]),
};

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
  const open = source.indexOf("{", i);
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
  // The two legacy strings may appear only inside the group branch.
  const composerNote = thread.indexOf("Sending unlocks with E2EE (milestone 4)");
  if (composerNote !== -1 && (groupGate === -1 || composerNote < groupGate)) {
    fail("[id].tsx shows the group composer note outside the group branch.");
  }
  const placeholderAt = thread.indexOf("Encrypted message");
  const groupMapAt = thread.search(/conversation\?\.kind\s*===\s*"group"\s*\?\s*messages\.map/);
  if (placeholderAt !== -1 && (groupMapAt === -1 || groupMapAt > placeholderAt)) {
    fail("[id].tsx shows the \"Encrypted message\" placeholder outside the group branch.");
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
