/** Execute the real deletion receipt lifecycle against delayed API/storage boundaries. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

function load(path, imports = {}) {
  const output = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", output)(name => {
    assert.ok(name in imports, `unexpected import ${name}`);
    return imports[name];
  }, module, module.exports);
  return module.exports;
}
const { DeletionCompletion } = load("../src/crypto/deletionCompletion.ts");
const USER = "00000000-0000-4000-8000-000000000001";
const OTHER = "00000000-0000-4000-8000-000000000002";
const REQUEST = "00000000-0000-4000-8000-000000000003";
const NOW = Date.parse("2026-10-27T12:00:00Z");
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function fixture() {
  const values = new Map();
  const events = [];
  let owner = { uid: "firebase-a", generation: 1 };
  const ports = {
    secure: {
      get: async key => values.get(key) ?? null,
      set: async (key, value) => { values.set(key, value); },
      remove: async key => { values.delete(key); },
    },
    now: () => NOW,
    currentIdentity: () => owner,
    ownsIdentity: captured => captured === owner,
    issue: async requestId => ({ user_id: USER, request_id: requestId, token: "read-only-proof", expires_at: new Date(NOW + 90 * 86_400_000).toISOString() }),
    check: async () => ({ user_id: USER, request_id: REQUEST, completed: true }),
    purge: async userId => { events.push(["purge", userId]); },
    afterPurge: async (record, captured) => {
      if (captured === owner && captured.uid === record.firebaseUid) events.push(["signout", captured.uid]);
    },
  };
  const coordinator = new DeletionCompletion(ports);
  return {
    ports, values, events, coordinator,
    remember: () => coordinator.remember(USER, REQUEST, owner),
    switch: uid => { owner = { uid, generation: owner.generation + 1 }; },
  };
}

let count = 0;
async function test(name, run) { await run(); count++; console.log(`PASS ${name}`); }

await test("confirmed completion wipes only bound account, then signs it out and removes receipt", async () => {
  const f = fixture(); await f.remember(); await f.coordinator.checkPending();
  assert.deepEqual(f.events, [["purge", USER], ["signout", "firebase-a"]]);
  await f.coordinator.checkPending(); assert.equal(f.events.length, 2);
});
await test("secure receipt survives a new coordinator and Firebase sign-out", async () => {
  const f = fixture(); await f.remember(); f.switch(null);
  await new DeletionCompletion(f.ports).checkPending();
  assert.deepEqual(f.events, [["purge", USER]]);
});
await test("a different signed-in account neither polls nor wipes the pending owner", async () => {
  const f = fixture(); await f.remember(); f.switch("firebase-b");
  f.ports.check = async () => { throw new Error("should not poll"); };
  await f.coordinator.checkPending(); assert.deepEqual(f.events, []);
});
for (const [name, check] of [
  ["pending", async () => ({ user_id: USER, request_id: REQUEST, completed: false })],
  ["foreign user", async () => ({ user_id: OTHER, request_id: REQUEST, completed: true })],
  ["foreign request", async () => ({ user_id: USER, request_id: OTHER, completed: true })],
  ["nonboolean truthy status", async () => ({ user_id: USER, request_id: REQUEST, completed: "true" })],
  ["401", async () => { throw Object.assign(new Error("unauthorized"), { status: 401 }); }],
  ["expired server proof", async () => { throw Object.assign(new Error("expired"), { status: 410 }); }],
  ["network failure", async () => { throw new TypeError("offline"); }],
]) await test(`${name} never authorizes erasure and retains receipt`, async () => {
  const f = fixture(); await f.remember(); f.ports.check = check;
  await f.coordinator.checkPending(); assert.deepEqual(f.events, []);
  assert.ok(f.values.get(`chirp.deletion.receipt.${REQUEST}`));
});
await test("account change while completion is in flight rejects the old answer", async () => {
  const f = fixture(); await f.remember(); const gate = deferred(); const reached = deferred();
  f.ports.check = async () => { reached.resolve(); return gate.promise; };
  const work = f.coordinator.checkPending(); await reached.promise; f.switch("firebase-b");
  gate.resolve({ user_id: USER, request_id: REQUEST, completed: true }); await work;
  assert.deepEqual(f.events, []);
});
await test("failed local wipe retries from persisted proof without signing out prematurely", async () => {
  const f = fixture(); await f.remember(); let attempts = 0;
  f.ports.purge = async userId => { if (++attempts === 1) throw new Error("store locked"); f.events.push(["purge", userId]); };
  await f.coordinator.checkPending(); assert.deepEqual(f.events, []);
  await f.coordinator.checkPending(); assert.equal(attempts, 2);
  assert.deepEqual(f.events, [["purge", USER], ["signout", "firebase-a"]]);
});
await test("account switch during local wipe cannot sign out the new account", async () => {
  const f = fixture(); await f.remember();
  f.ports.purge = async userId => { f.events.push(["purge", userId]); f.switch("firebase-b"); };
  await f.coordinator.checkPending(); assert.deepEqual(f.events, [["purge", USER]]);
});
await test("simultaneous foreground checks share one request and one purge", async () => {
  const f = fixture(); await f.remember(); let requests = 0;
  f.ports.check = async () => { requests++; return { user_id: USER, request_id: REQUEST, completed: true }; };
  await Promise.all([f.coordinator.checkPending(), f.coordinator.checkPending()]);
  assert.equal(requests, 1); assert.equal(f.events.filter(e => e[0] === "purge").length, 1);
});
await test("expired local proof is preserved without polling or wiping", async () => {
  const f = fixture(); await f.remember(); f.ports.now = () => NOW + 91 * 86_400_000;
  f.ports.check = async () => { throw new Error("should not poll"); };
  await f.coordinator.checkPending(); assert.deepEqual(f.events, []);
  assert.ok(f.values.get(`chirp.deletion.receipt.${REQUEST}`));
});
await test("minted proof must match both captured user and request", async () => {
  const f = fixture(); f.ports.issue = async () => ({ user_id: OTHER, request_id: REQUEST, token: "foreign", expires_at: new Date(NOW + 90 * 86_400_000).toISOString() });
  await assert.rejects(f.remember()); assert.equal(f.values.size, 0);
});
await test("account switch during mint does not persist the old response", async () => {
  const f = fixture(); const issue = f.ports.issue;
  f.ports.issue = async id => { const response = await issue(id); f.switch("firebase-b"); return response; };
  await assert.rejects(f.remember()); assert.equal(f.values.size, 0);
});
await test("corrupt secure index fails closed", async () => {
  const f = fixture(); f.values.set("chirp.deletion.pending.v1", "not-json");
  await assert.rejects(f.coordinator.checkPending()); assert.deepEqual(f.events, []);
});
await test("expired and missing receipts cannot permanently fill the pending index", async () => {
  const f = fixture();
  const ids = Array.from({ length: 16 }, (_, i) => `00000000-0000-4000-8000-${String(100 + i).padStart(12, "0")}`);
  f.values.set("chirp.deletion.pending.v1", JSON.stringify(ids));
  for (const id of ids.slice(0, 8)) f.values.set(`chirp.deletion.receipt.${id}`, JSON.stringify({
    userId: USER, requestId: id, firebaseUid: "firebase-a", token: "expired", expiresAt: new Date(NOW - 1).toISOString(),
  }));
  await f.remember();
  assert.deepEqual(JSON.parse(f.values.get("chirp.deletion.pending.v1")), [REQUEST]);
  assert.deepEqual(f.events, []);
});
await test("actual API wrappers forbid redirects and completion does not refresh Firebase", async () => {
  const calls = [];
  const api = load("../src/api/dataRequests.ts", { "./client": { request: async (...args) => { calls.push(args); return {}; }, requestText: async () => "" } });
  await api.createDeletionCompletionToken(REQUEST); await api.checkDeletionCompletion("secret-proof");
  assert.equal(calls[0][0], `/me/data-requests/${REQUEST}/completion-token`);
  assert.equal(calls[0][1].redirect, "error");
  assert.equal(calls[1][0], "/account-deletion/completion");
  assert.deepEqual(calls[1][1], { method: "POST", body: { token: "secret-proof" }, retryAuth: false, redirect: "error" });
});
await test("real client selects installed Expo fetch and forwards redirect error to native transport", async () => {
  const identity = load("../src/auth/identity.ts");
  const operation = load("../src/api/operation.ts", { "../auth/identity": identity });
  const errors = load("../node_modules/expo/src/winter/fetch/FetchErrors.ts");
  const starts = [];
  const expo = load("../node_modules/expo/src/winter/fetch/fetch.ts", {
    "./ExpoFetchModule": { ExpoFetchModule: { NativeRequest: class {
      async start(url, init, body) { starts.push({ url, init, body }); throw new Error("controlled_redirect_refused"); }
      cancel() {}
    } } },
    "./FetchErrors": errors,
    "./FetchResponse": { FetchResponse: class {} },
    "./RequestUtils": { normalizeHeadersInit: headers => headers, normalizeBodyInitAsync: async body => ({ body }), overrideHeaders: () => { throw new Error("unexpected override"); } },
  });
  const client = load("../src/api/client.ts", {
    "../auth/config": { hasFirebaseConfig: () => false },
    "../auth/session": { captureSession: identity.currentIdentity, getIdToken: () => { throw new Error("must not refresh"); } },
    "../auth/identity": identity, "./operation": operation, "expo/fetch": expo,
  });
  const oldFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("unsafe_global_fetch_selected"); };
  try {
    await assert.rejects(client.request("/account-deletion/completion", {
      method: "POST", body: { token: "private-proof" }, retryAuth: false, redirect: "error",
    }), /controlled_redirect_refused/);
  } finally { globalThis.fetch = oldFetch; }
  assert.equal(starts.length, 1);
  assert.equal(starts[0].init.redirect, "error");
  assert.equal(starts[0].init.method, "POST");
  assert.equal(starts[0].body, '{"token":"private-proof"}');
});
await test("an iOS build without the native bridge retains proof until a later build can wipe", async () => {
  const f = fixture(); let available = false; let erased = 0; let signedOut = 0;
  const runtime = load("../src/crypto/deletionCompletionRuntime.ts", {
    "expo-secure-store": {
      getItemAsync: f.ports.secure.get, setItemAsync: f.ports.secure.set, deleteItemAsync: f.ports.secure.remove,
      AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY: "device-only",
    },
    "react-native": { Platform: { OS: "ios" } },
    "../api/dataRequests": { createDeletionCompletionToken: f.ports.issue, checkDeletionCompletion: f.ports.check },
    "../auth/identity": { currentIdentity: f.ports.currentIdentity, ownsIdentity: f.ports.ownsIdentity },
    "../auth/session": { signOutUser: async () => { signedOut++; } },
    "./deletionCompletion": { DeletionCompletion },
    "./e2eeRuntime": { e2ee: { purgeAccount: async () => { if (!available) throw new Error("native unavailable"); erased++; } } },
  });
  // Keep the runtime's clock aligned with the issued receipt's fixed test epoch.
  const now = Date.now;
  Date.now = () => NOW;
  try {
    assert.equal(await runtime.rememberDeletion(USER, REQUEST, f.ports.currentIdentity()), true);
    await runtime.checkPendingDeletions(); assert.equal(erased, 0); assert.equal(signedOut, 0);
    assert.ok(f.values.get(`chirp.deletion.receipt.${REQUEST}`));
    available = true; await runtime.checkPendingDeletions(); assert.equal(erased, 1); assert.equal(signedOut, 1);
  } finally { Date.now = now; }
});
function hookHarness() {
  const slots = [];
  let cursor = 0;
  let effects = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    createContext: value => ({ value }), useContext: context => context.value,
    useState(initial) {
      const index = cursor++;
      slots[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [slots[index].value, value => { slots[index].value = typeof value === "function" ? value(slots[index].value) : value; }];
    },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
    useMemo(work, deps) {
      const index = cursor++;
      if (!slots[index] || !same(slots[index].deps, deps)) slots[index] = { deps, value: work() };
      return slots[index].value;
    },
    useCallback(work, deps) { return hooks.useMemo(() => work, deps); },
    useEffect(work, deps) {
      const index = cursor++;
      if (!slots[index] || !same(slots[index].deps, deps)) {
        const prior = slots[index]; slots[index] = { deps };
        effects.push(() => { prior?.cleanup?.(); slots[index].cleanup = work(); });
      }
    },
  };
  const listeners = new Set();
  const page = deferred();
  const ready = { status: "ready", userId: USER };
  const empty = { messages: [], hasOlder: false, outbox: [], undecryptable: [] };
  let purged = false;
  const nativeRow = seq => ({ seq, body: `private-${seq}`, senderUserId: USER, clientMessageId: String(seq), direction: "outgoing", storedAtMs: NOW });
  const engine = {
    getState: () => ready,
    loadThread: async (_id, before) => before === undefined
      ? (purged ? empty : { ...empty, messages: [nativeRow(10)], hasOlder: true }) : page.promise,
    syncConversation: async () => {}, trustFor: async () => ({ block: null, verified: false, fingerprint: null }),
    subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
  };
  const types = load("../src/crypto/e2eeTypes.ts");
  const mapping = load("../src/crypto/e2eeMapping.ts", { "./e2eeTypes": types });
  const thread = load("../src/crypto/e2eeThread.ts", { "./e2eeMapping": mapping });
  const provider = load("../src/crypto/E2eeProvider.tsx", {
    react: hooks, "react/jsx-runtime": { jsx: () => null },
    "react-native": { AppState: { currentState: "active" } }, "@/auth": {},
    "@/realtime/socket": { chirpSocket: { onStatus: () => () => {} } },
    "./e2eeThread": thread, "./e2eeRuntime": { e2ee: engine }, "./e2eeTrust": {}, "./e2eeTypes": types,
  });
  let user = USER;
  const render = () => {
    cursor = 0; effects = [];
    const result = provider.useEncryptedDm(REQUEST, OTHER, user);
    for (const effect of effects) effect();
    return result;
  };
  return {
    render, page, nativeRow,
    purge: () => { purged = true; for (const fn of listeners) fn({ type: "purged", conversationId: REQUEST }); },
    switch: () => { user = OTHER; },
  };
}
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
await test("purge clears real thread hook and fences a late older-page response", async () => {
  const f = hookHarness(); f.render(); await settle();
  let thread = f.render(); assert.equal(thread.items[0].body, "private-10");
  const older = thread.loadOlder(); await settle();
  f.purge(); assert.deepEqual(f.render().items, []);
  f.page.resolve({ messages: [f.nativeRow(1)], hasOlder: false, outbox: [], undecryptable: [] });
  await older; await settle(); assert.deepEqual(f.render().items, []);
});
await test("real thread hook exposes no old-account plaintext on first account-switch render", async () => {
  const f = hookHarness(); f.render(); await settle(); assert.equal(f.render().items.length, 1);
  f.switch(); assert.deepEqual(f.render().items, []);
  await settle(); assert.deepEqual(f.render().items, []);
});
console.log(`verify:deletion-completion — ${count} actual-source cases passed`);
