/** Executed c343 regressions. Real TS handlers, fake clocks/Firebase/network/native boundaries.
 * No live credentials, network, emulator, or new test dependency. This proves client
 * ownership/deadline behavior; native provider/device integration remains a device check.
 */
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import ts from "typescript";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
const response = (status = 200, data = {}) => ({ status, ok: status >= 200 && status < 300, statusText: "Failure", headers: new Headers(), json: async () => data, text: async () => JSON.stringify(data) });
const account = uid => ({ id: `id-${uid}`, firebase_uid: uid, campus_id: "campus", suspended_at: null });

function clock() {
  let now = 0, next = 0;
  const tasks = new Map();
  return {
    setTimeout: (fn, ms) => { const id = ++next; tasks.set(id, { fn, due: now + ms }); return id; },
    clearTimeout: id => tasks.delete(id),
    async tick(ms) {
      const end = now + ms;
      while (true) {
        const first = [...tasks].sort((a, b) => a[1].due - b[1].due)[0];
        if (!first || first[1].due > end) break;
        tasks.delete(first[0]); now = first[1].due; first[1].fn(); await flush();
      }
      now = end; await flush();
    },
  };
}

// Hook scheduler executes the actual component render/effect/cleanup callbacks.
// It intentionally does not imitate the production auth logic under test.
function hooks() {
  const slots = []; let cursor = 0, pending = [], queued = false, alive = true, component, props, tree;
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  function schedule() { if (!queued && alive) { queued = true; queueMicrotask(render); } }
  function render() {
    queued = false; if (!alive || !component) return;
    cursor = 0; pending = []; tree = component(props);
    for (const effect of pending) effect();
  }
  const react = {
    createContext: () => ({ Provider: "Provider" }),
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
      return [slots[i], value => { const next = typeof value === "function" ? value(slots[i]) : value; if (!Object.is(next, slots[i])) { slots[i] = next; schedule(); } }];
    },
    useRef(initial) { const i = cursor++; return slots[i] ??= { current: initial }; },
    useMemo(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { deps, value: fn() }; return slots[i].value; },
    useCallback(fn, deps) { return react.useMemo(() => fn, deps); },
    useEffect(fn, deps) {
      const i = cursor++, old = slots[i];
      if (!old || !same(old.deps, deps)) pending.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; });
    },
  };
  return { react, mount(fn, input = {}) { component = fn; props = input; render(); }, update(input) { props = input; render(); }, get tree() { return tree; }, get value() { return tree.props.value; }, unmount() { alive = false; for (const value of slots) value?.cleanup?.(); } };
}

function environment() {
  const timer = clock(), hook = hooks(), auth = { currentUser: null };
  const authListeners = new Set(), tokenListeners = new Set(), beforeListeners = new Set();
  const calls = [], sockets = [], alerts = [], cache = new Map();
  const env = { timer, hook, auth, calls, sockets, alerts, signOutCalls: 0, pickerCalls: 0, commits: [] };
  env.fetch = async (url) => {
    if (url.endsWith("/auth/me")) return response(200, { user: account(auth.currentUser?.uid), memberships: [] });
    if (url.endsWith("/auth/campus-verification")) return response(200, { verified: false });
    return response();
  };
  env.user = (uid, token = async () => `token-${uid}`) => ({ uid, getIdToken: token });
  env.emit = user => { auth.currentUser = user; for (const fn of authListeners) fn(user); };
  env.emitToken = user => { for (const fn of tokenListeners) fn(user); };
  // Matches the installed Firebase SDK boundary: pre-commit callbacks run BEFORE
  // currentUser changes and before the credential/signOut promise resolves.
  env.commitUser = async user => { for (const fn of beforeListeners) await fn(user); env.commits.push(user?.uid ?? null); env.emit(user); };
  env.signOut = async () => { await env.commitUser(null); };
  env.signIn = async () => { throw new Error("Test must supply sign-in"); };
  env.permission = async () => ({ granted: true });
  env.picker = async () => ({ canceled: false, assets: [{ uri: "file://photo", mimeType: "image/jpeg", fileSize: 3 }] });
  class WebSocket {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; sockets.push(this); }
    close() { this.closed = true; }
  }
  const jsx = (type, props) => ({ type, props });
  const stubs = {
    "react": hook.react,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "Fragment" },
    "firebase/auth": {
      beforeAuthStateChanged: (_, fn) => { beforeListeners.add(fn); return () => beforeListeners.delete(fn); },
      onAuthStateChanged: (_, fn) => { authListeners.add(fn); return () => authListeners.delete(fn); },
      onIdTokenChanged: (_, fn) => { tokenListeners.add(fn); return () => tokenListeners.delete(fn); },
      signOut: async () => { env.signOutCalls++; return env.signOut(); },
      signInWithEmailAndPassword: (...args) => env.signIn(...args),
      createUserWithEmailAndPassword: (...args) => env.signIn(...args),
    },
    "react-native": { View: "View", Pressable: "Pressable", Modal: "Modal", Image: "Image", TextInput: "TextInput", ActivityIndicator: "ActivityIndicator", KeyboardAvoidingView: "KeyboardAvoidingView", Platform: { OS: "ios" } },
    "react-native-safe-area-context": { useSafeAreaInsets: () => ({ top: 0, bottom: 0 }) },
    "@expo/vector-icons": { Feather: "Feather" },
    "expo-router": { Redirect: "Redirect", Tabs: Object.assign(() => {}, { Screen: "Tabs.Screen" }) },
    "react-native-reanimated": { default: {}, interpolate() {}, useAnimatedStyle() {} },
    [resolve(ROOT, "src/nav/TabBarVisibility.tsx")]: { TabBarVisibilityProvider: "TabBarVisibilityProvider" },
    [resolve(ROOT, "src/components/index.ts")]: { AppText: "AppText", EmptyState: "EmptyState", Screen: "Screen" },
    "expo-image-picker": { requestMediaLibraryPermissionsAsync: () => env.permission(), launchImageLibraryAsync: () => { env.pickerCalls++; return env.picker(); } },
    [resolve(ROOT, "src/auth/config.ts")]: { hasFirebaseConfig: () => true },
    [resolve(ROOT, "src/auth/firebase.ts")]: { getFirebaseAuth: () => auth },
    [resolve(ROOT, "src/auth/devAuth.ts")]: { devAuthUid: () => null },
    [resolve(ROOT, "src/lib/alert.ts")]: { showAlert: (...args) => alerts.push(args), showApiError: (...args) => alerts.push(args) },
    [resolve(ROOT, "src/auth/index.ts")]: { useCampusAccess: () => "verified", useSession: () => hook.value },
    [resolve(ROOT, "src/theme/index.ts")]: { useTheme: () => ({}), light: {}, radii: {}, spacing: {}, inputField: () => ({}), withAlpha: () => "color" },
  };
  const context = vm.createContext({ AbortController, Headers, Promise, console, queueMicrotask,
    setTimeout: timer.setTimeout, clearTimeout: timer.clearTimeout, WebSocket,
    process: { env: {} }, fetch: (...args) => { calls.push(args); return env.fetch(...args); },
  });
  function load(specifier, from = resolve(ROOT, "entry.ts")) {
    if (specifier in stubs) return stubs[specifier];
    const base = specifier.startsWith("@/") ? resolve(ROOT, "src", specifier.slice(2)) : resolve(dirname(from), specifier);
    const path = [base, base + ".ts", base + ".tsx", base + "/index.ts"].find(p => existsSync(p) && /\.[cm]?[jt]sx?$/.test(p));
    if (!path) throw new Error(`Missing test boundary: ${specifier}`);
    if (path in stubs) return stubs[path];
    if (path.includes("/components/") && !path.endsWith("/CreateSheet.tsx")) {
      const name = path.split("/").at(-1).replace(/\.tsx?$/, ""); return { [name]: name };
    }
    if (cache.has(path)) return cache.get(path).exports;
    const module = { exports: {} }; cache.set(path, module);
    const { outputText } = ts.transpileModule(readFileSync(path, "utf8"), { fileName: path, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } });
    const run = vm.runInContext(`(function(require, module, exports) {${outputText}
})`, context, { filename: path });
    run(name => load(name, path), module, module.exports); return module.exports;
  }
  env.load = name => load("./" + name);
  env.identity = env.load("src/auth/identity.ts");
  env.session = env.load("src/auth/session.ts");
  env.client = env.load("src/api/client.ts");
  env.operation = env.load("src/api/operation.ts");
  env.mountProvider = async () => { hook.mount(env.load("src/auth/SessionProvider.tsx").SessionProvider, { children: null }); await flush(); };
  env.close = () => { hook.unmount(); cache.get(resolve(ROOT, "src/realtime/socket.ts"))?.exports.chirpSocket.disconnect(); env.identity.replaceIdentity(null, true); };
  return env;
}

let count = 0;
async function test(name, fn) { const env = environment(); try { await fn(env); console.log(`PASS ${name}`); count++; } finally { env.close(); await flush(); } }
const settled = promise => promise.then(value => ({ value }), error => ({ error }));
const nodes = tree => !tree || typeof tree !== "object" ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)];

await test("slow A token cannot reinstall after logout or block B", async e => {
  const a = deferred(); e.emit(e.user("A", () => a.promise));
  const old = settled(e.session.getIdToken()); await flush();
  await e.session.signOutUser(); e.emit(e.user("B"));
  assert.equal(await e.session.getIdToken(), "token-B");
  a.resolve("late-A"); await flush();
  assert.ok((await old).error); assert.equal(e.identity.tokenFor(), "token-B");
});

await test("same UID logout/relogin has new generation; routine token callback keeps it", async e => {
  e.emit(e.user("A")); await e.session.getIdToken();
  const old = e.identity.currentIdentity(); await e.session.signOutUser();
  e.emit(e.user("A")); await e.session.getIdToken();
  assert.notEqual(e.identity.currentIdentity().generation, old.generation);
  const generation = e.identity.currentIdentity().generation;
  const stop = e.session.onIdTokenChanged(); e.emitToken(e.auth.currentUser); await flush(); stop();
  assert.equal(e.identity.currentIdentity().generation, generation);
});

await test("concurrent 401 requests share forced refresh and preserve each response", async e => {
  let refreshes = 0; const refresh = deferred();
  e.emit(e.user("A", force => force ? (++refreshes, refresh.promise) : Promise.resolve("old")));
  await e.session.getIdToken();
  e.fetch = async (url, options) => options.headers.Authorization === "Bearer old" ? response(401) : response(200, { url });
  const a = e.client.request("/one"), b = e.client.request("/two"); await flush();
  assert.equal(refreshes, 1); refresh.resolve("fresh");
  assert.ok((await a).url.endsWith("/one")); assert.ok((await b).url.endsWith("/two"));
  assert.equal(e.calls.length, 4);
});

await test("a stalled refresh times out; an expired SDK result cannot replace later token", async e => {
  const stalled = deferred(); let calls = 0;
  e.emit(e.user("A", () => ++calls === 1 ? stalled.promise : Promise.resolve("new")));
  const old = settled(e.session.getIdToken(true)); await flush();
  await e.timer.tick(15_000); assert.equal((await old).error.name, "OperationTimeoutError");
  assert.equal(await e.session.getIdToken(true), "new");
  stalled.resolve("expired"); await flush(); assert.equal(e.identity.tokenFor(), "new");
});

await test("SDK synchronous throw clears refresh slot for a later retry", async e => {
  e.emit(e.user("A", () => { throw new Error("offline"); }));
  assert.ok((await settled(e.session.getIdToken(true))).error);
  e.auth.currentUser.getIdToken = async () => "recovered";
  assert.equal(await e.session.getIdToken(true), "recovered");
});

await test("request deadline includes first fetch, forced refresh, retry and body decoding", async e => {
  const first = deferred(), refresh = deferred(), body = deferred();
  e.emit(e.user("A", force => force ? refresh.promise : Promise.resolve("old"))); await e.session.getIdToken();
  e.fetch = () => e.calls.length === 1 ? first.promise : Promise.resolve({ ...response(), json: () => body.promise });
  const result = settled(e.client.request("/slow", { timeoutMs: 1_000 }));
  await e.timer.tick(400); first.resolve(response(401)); await flush();
  await e.timer.tick(400); refresh.resolve("fresh"); await flush();
  assert.equal(e.calls.length, 2); await e.timer.tick(200);
  assert.equal((await result).error.name, "OperationTimeoutError");
  assert.equal(e.calls[1][1].signal.aborted, true);
  body.resolve({ late: true }); await flush();
});

await test("deadline stops a non-cooperative fetch and caller cancellation stops refresh retry", async e => {
  e.emit(e.user("A")); await e.session.getIdToken(); e.fetch = () => new Promise(() => {});
  const timed = settled(e.client.requestText("/stalled", { timeoutMs: 50 })); await e.timer.tick(50);
  assert.equal((await timed).error.name, "OperationTimeoutError");
  const refresh = deferred(); e.auth.currentUser.getIdToken = () => refresh.promise; e.fetch = async () => response(401);
  const caller = new AbortController(), cancelled = settled(e.client.request("/cancel", { signal: caller.signal })); await flush();
  caller.abort(); assert.equal((await cancelled).error.name, "OperationCancelledError");
  refresh.resolve("fresh"); await flush(); assert.equal(e.calls.length, 2);
});

await test("A 401 refresh never retries with B credentials; ordinary ApiError is unchanged", async e => {
  const refresh = deferred(); e.emit(e.user("A", force => force ? refresh.promise : Promise.resolve("old"))); await e.session.getIdToken();
  e.fetch = async () => response(401); const pending = settled(e.client.request("/private")); await flush();
  e.emit(e.user("B")); await e.session.getIdToken(); refresh.resolve("late-A"); await flush();
  assert.ok((await pending).error); assert.equal(e.calls.length, 1); assert.equal(e.identity.tokenFor(), "token-B");
  e.fetch = async () => response(403, { detail: "campus_verification_required" });
  const failure = (await settled(e.client.request("/forbidden"))).error;
  assert.ok(failure instanceof e.client.ApiError); assert.equal(failure.detail, "campus_verification_required");
});

await test("returning user gets bounded recovery from stalled token, retry succeeds without logout", async e => {
  const token = deferred(); e.emit(e.user("A", () => token.promise)); await e.mountProvider();
  assert.equal(e.hook.value.status, "loading"); await e.timer.tick(10_000);
  assert.equal(e.hook.value.status, "recoverable"); assert.equal(e.signOutCalls, 0);
  token.resolve("token-A"); await flush(); assert.equal(e.hook.value.status, "recoverable");
  assert.equal(await e.hook.value.refresh(), true); await flush(); assert.equal(e.hook.value.status, "ready");
});

await test("failed /me and failed manual retry never invent onboarding or logout", async e => {
  e.emit(e.user("A")); e.fetch = async () => response(503); await e.mountProvider();
  assert.equal(e.hook.value.status, "recoverable");
  assert.equal(await e.hook.value.refresh(), false); await flush(); assert.equal(e.hook.value.status, "recoverable");
  e.fetch = async () => response(404, { detail: "other_missing_route" }); await e.hook.value.refresh(); await flush();
  assert.equal(e.hook.value.status, "recoverable"); assert.equal(e.signOutCalls, 0);
  e.fetch = async () => response(404, { detail: "user_not_registered" }); await e.hook.value.refresh(); await flush();
  assert.equal(e.hook.value.status, "unregistered");
});

await test("late A /me cannot publish into B; stale bootstrap rejected", async e => {
  const me = deferred(); e.emit(e.user("A")); e.fetch = () => me.promise; await e.mountProvider();
  const staleBootstrap = e.hook.value.applyBootstrap;
  e.fetch = async url => url.endsWith("/auth/me") ? response(200, { user: account("B"), memberships: [] }) : response(200, { verified: false, name: "B campus" });
  e.emit(e.user("B")); await flush(); assert.equal(e.hook.value.user.firebase_uid, "B");
  me.resolve(response(200, { user: account("A"), memberships: [] })); staleBootstrap(account("A")); await flush();
  assert.equal(e.hook.value.user.firebase_uid, "B");
  assert.equal(e.hook.value.campus.name, "B campus");
});

await test("Q c379 redeem publication beats stale GET and cannot cross account generation", async e => {
  const verification = deferred(); e.emit(e.user("A"));
  const normalFetch = e.fetch; e.fetch = url => url.endsWith("/auth/campus-verification") ? verification.promise : normalFetch(url);
  await e.mountProvider(); const publishA = e.hook.value.applyCampusVerification;
  publishA({ verified: true }); await flush();
  verification.resolve(response(200, { verified: false })); await flush(); assert.equal(e.hook.value.campusVerification.verified, true);
  e.fetch = normalFetch; e.emit(e.user("B")); await flush();
  publishA({ verified: true }); await flush(); assert.equal(e.hook.value.campusVerification.verified, false);
});

await test("old socket open/message/close cannot overwrite B socket or schedule reconnect", async e => {
  e.emit(e.user("A")); await e.session.getIdToken();
  const socket = e.load("src/realtime/socket.ts").chirpSocket; const events = []; socket.onEvent(v => events.push(v));
  socket.connect(); const a = e.sockets[0]; a.onopen();
  e.emit(e.user("B")); await e.session.getIdToken(); assert.equal(a.closed, true);
  socket.connect(); const b = e.sockets[1]; b.onopen();
  a.onopen(); a.onmessage({ data: '{"type":"message"}' }); a.onclose({ code: 4403 });
  assert.equal(socket.getStatus(), "open"); assert.equal(events.length, 0);
  await e.timer.tick(35_000); assert.equal(e.sockets.length, 2);
  b.onmessage({ data: '{"type":"message"}' }); assert.equal(events.length, 1);
});

await test("media URL and PUT share one deadline and PUT carries no Firebase bearer", async e => {
  e.emit(e.user("A")); await e.session.getIdToken(); const url = deferred(), put = deferred();
  e.fetch = address => address.includes("/media/upload-url") ? url.promise : put.promise;
  const media = e.load("src/api/media.ts"), operation = new e.operation.Operation({ timeoutMs: 1_000 });
  const flow = settled((async () => { const signed = await media.getMediaUploadUrl("image/jpeg", 3, { operation }); await media.uploadMediaBytes(signed.upload_url, {}, "image/jpeg", { operation }); })());
  await e.timer.tick(700); url.resolve(response(200, { upload_url: "https://storage.test/object" })); await flush();
  assert.equal(e.calls[1][1].headers.Authorization, undefined);
  assert.equal(e.calls[1][1].headers["X-Goog-Content-Length-Range"], "1,10485760");
  await e.timer.tick(300); assert.equal((await flow).error.name, "OperationTimeoutError");
  assert.equal(e.calls[1][1].signal.aborted, true); put.resolve(response()); operation.dispose();
});

async function mountSheet(e) {
  e.emit(e.user("A")); await e.session.getIdToken();
  e.hook.mount(e.load("src/components/CreateSheet.tsx").CreateSheet, { visible: true, chapterId: "chapter", campusId: "campus", onClose() {} }); await flush();
  const photo = nodes(e.hook.tree).find(n => n.type === "ListRow" && n.props.title === "Photo");
  assert.ok(photo, "actual Photo row is reachable"); photo.props.onPress(); await flush();
}
await test("closing composer aborts PUT and ignores its late success", async e => {
  const put = deferred(); e.fetch = async address => address.startsWith("file:") ? { blob: async () => ({ size: 3 }) } : address.includes("/media/upload-url") ? response(200, { upload_url: "https://storage.test/object", object_name: "tmp/object" }) : put.promise;
  await mountSheet(e); assert.equal(e.calls.length, 3);
  nodes(e.hook.tree).find(n => n.props?.accessibilityLabel === "Close").props.onPress(); await flush();
  assert.equal(e.calls[2][1].signal.aborted, true); put.resolve(response()); await flush();
  assert.equal(nodes(e.hook.tree).filter(n => n.type === "Image").length, 0); assert.equal(e.alerts.length, 0);
});
await test("unmount during signed URL wait prevents PUT and any late upload alert", async e => {
  const url = deferred(); e.fetch = async address => address.startsWith("file:") ? { blob: async () => ({ size: 3 }) } : url.promise;
  await mountSheet(e); e.hook.unmount(); url.resolve(response(200, { upload_url: "https://storage.test/object" })); await flush();
  assert.equal(e.calls.length, 2); assert.equal(e.alerts.length, 0);
});
await test("dismissal while OS permission is pending never opens a late picker", async e => {
  const permission = deferred(); e.permission = () => permission.promise; await mountSheet(e);
  nodes(e.hook.tree).find(n => n.props?.accessibilityLabel === "Close").props.onPress();
  permission.resolve({ granted: true }); await flush(); assert.equal(e.pickerCalls, 0); assert.equal(e.calls.length, 0);
});

await test("late secondary fetches from A cannot repaint B, even on the same campus", async e => {
  const campusA = deferred(), verificationA = deferred(); e.emit(e.user("A"));
  const normalFetch = e.fetch;
  e.fetch = url => url.endsWith("/campuses/campus") ? campusA.promise : url.endsWith("/auth/campus-verification") ? verificationA.promise : normalFetch(url);
  await e.mountProvider(); assert.equal(e.hook.value.status, "ready");
  e.fetch = async url => url.endsWith("/auth/me") ? response(200, { user: account("B"), memberships: [] }) : response(200, { name: "B campus", verified: false });
  e.emit(e.user("B")); await flush();
  campusA.resolve(response(200, { name: "old A campus" })); verificationA.resolve(response(200, { verified: true })); await flush();
  assert.equal(e.hook.value.campus.name, "B campus"); assert.equal(e.hook.value.campusVerification.verified, false);
});

await test("startup /me timeout renders an actionable recovery screen; its button retries", async e => {
  const me = deferred(); e.emit(e.user("A")); e.fetch = () => me.promise; await e.mountProvider();
  await e.timer.tick(10_000); assert.equal(e.hook.value.status, "recoverable");
  const layout = e.load("app/(tabs)/_layout.tsx").default();
  const retry = nodes(layout).find(n => n.type === "EmptyState");
  assert.equal(retry.props.actionLabel, "Try again"); assert.equal(nodes(layout).some(n => n.type === "Redirect"), false);
  e.fetch = async url => url.endsWith("/auth/me") ? response(200, { user: account("A"), memberships: [] }) : response(200, { verified: false });
  retry.props.onAction(); await flush(); assert.equal(e.hook.value.status, "ready");
  me.resolve(response(503)); await flush(); assert.equal(e.hook.value.status, "ready");
});

await test("missing initial Firebase callback is bounded and explicit logout clears ready session", async e => {
  await e.mountProvider(); await e.timer.tick(10_000); assert.equal(e.hook.value.status, "signedOut");
  e.emit(e.user("A")); await flush(); assert.equal(e.hook.value.status, "ready");
  await e.session.signOutUser(); await flush(); assert.equal(e.hook.value.status, "signedOut");
  assert.equal(e.hook.value.user, null); assert.equal(e.hook.value.campusVerification, null);
});

await test("same-UID late credential helper loses to a newer sign-in intent", async e => {
  const credential = deferred(), user = e.user("A"); e.emit(user); await e.session.getIdToken();
  e.signIn = () => credential.promise;
  const old = settled(e.session.signInWithEmail("unused", "unused")); await flush();
  const logout = settled(e.session.signOutUser());
  e.signIn = async () => { await e.commitUser(user); return { user }; };
  const next = e.session.signInWithEmail("unused", "unused");
  credential.resolve({ user }); await next; await logout; await flush(); assert.equal((await old).error.name, "SessionChangedError");
  assert.equal(e.identity.tokenFor(), "token-A");
});

await test("account switch during photo upload aborts it and clears the old composer", async e => {
  const put = deferred(); e.fetch = async address => address.startsWith("file:") ? { blob: async () => ({ size: 3 }) } : address.includes("/media/upload-url") ? response(200, { upload_url: "https://storage.test/object", object_name: "tmp/A" }) : put.promise;
  await mountSheet(e); e.emit(e.user("B")); await e.session.getIdToken(); await flush();
  assert.equal(e.calls[2][1].signal.aborted, true); put.resolve(response()); await flush();
  assert.equal(nodes(e.hook.tree).filter(n => n.type === "Image").length, 0); assert.equal(e.alerts.length, 0);
});

await test("explicit same-UID sign-in renews ownership before credential resolution", async e => {
  e.emit(e.user("A")); await e.session.getIdToken(); const owner = e.identity.currentIdentity();
  const attempt = e.session.beginSignIn();
  assert.equal(e.identity.ownsIdentity(owner), false); assert.equal(e.identity.currentIdentity().uid, null);
  attempt.assertCurrent();
  const pendingOwner = e.identity.currentIdentity();
  e.session.beginSignIn();
  assert.equal(e.identity.ownsIdentity(pendingOwner), false, "another action renews even the pending null-UID generation");
  await e.session.signOutUser(); assert.throws(attempt.assertCurrent, { name: "SessionChangedError" });
});

await test("late SDK A commit is vetoed after logout and B waits for the mutation to settle", async e => {
  const networkA = deferred(); let sdkCalls = 0;
  e.signIn = async () => { sdkCalls++; await networkA.promise; const user = e.user("A"); await e.commitUser(user); return { user }; };
  const old = settled(e.session.signInWithEmail("A", "unused")); await flush();
  const logout = settled(e.session.signOutUser());
  e.signIn = async () => { sdkCalls++; const user = e.user("B"); await e.commitUser(user); return { user }; };
  const next = e.session.signInWithEmail("B", "unused"); await flush();
  assert.equal(sdkCalls, 1); assert.equal(e.identity.tokenFor(), null);
  networkA.resolve(); await next; await logout;
  assert.ok((await old).error); assert.equal(e.auth.currentUser.uid, "B"); assert.equal(e.identity.tokenFor(), "token-B");
  assert.deepEqual(e.commits, ["B"], "stale A never reaches the SDK commit");
});

await test("auth queue wait is finite but caller timeout does not release stale SDK ownership", async e => {
  const networkA = deferred(); let sdkCalls = 0;
  e.signIn = async () => { sdkCalls++; await networkA.promise; const user = e.user("A"); await e.commitUser(user); return { user }; };
  const old = settled(e.session.signInWithEmail("A", "unused")); await flush(); await e.timer.tick(15_000);
  assert.equal((await old).error.name, "OperationTimeoutError");
  e.signIn = async () => { sdkCalls++; const user = e.user("B"); await e.commitUser(user); return { user }; };
  const next = settled(e.session.signInWithEmail("B", "unused")); await flush(); await e.timer.tick(15_000);
  assert.equal((await next).error.name, "OperationTimeoutError"); assert.equal(sdkCalls, 1);
  networkA.resolve(); await flush(); assert.equal(e.auth.currentUser, null); assert.equal(e.identity.tokenFor(), null);
  await e.session.signInWithEmail("B", "unused"); assert.equal(e.identity.tokenFor(), "token-B");
});

await test("SDK commit that passed its veto before logout still cannot publish a stale app session", async e => {
  const persist = deferred();
  e.signIn = async () => { const user = e.user("A"); await e.commitUser(user); await persist.promise; return { user }; };
  const old = settled(e.session.signInWithEmail("A", "unused")); await flush();
  const logout = e.session.signOutUser(); await flush();
  assert.equal(e.session.captureSession().uid, null); assert.equal(await e.session.getIdToken(), null);
  persist.resolve(); await logout; assert.ok((await old).error); assert.equal(e.auth.currentUser, null);
});

await test("auth deadline maps to safe retry copy instead of suggesting wrong credentials", async e => {
  const mapper = e.load("src/auth/authErrors.ts").getAuthErrorMessage;
  const message = mapper(new e.operation.OperationTimeoutError(), "signin");
  assert.match(message, /timed out.*try again/i); assert.doesNotMatch(message, /password/i);
});

console.log(`ALL PASS: ${count} executed c343 behavior regressions (TypeScript ${ts.version}).`);
