/** Execute c438's production TSX screens with deterministic React/API seams. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const identity = uid => ({ uid, generation: uid ? uid.charCodeAt(0) : 0 });
const policy = { required: true, material_change: false, accepted_policy_ids: [], policies: [
  { key: "privacy", version: "privacy-1", effective_at: "2026-10-01T00:00:00Z" },
  { key: "terms", version: "terms-1", effective_at: "2026-10-01T00:00:00Z" },
] };
class HarnessApiError extends Error { constructor(status, detail) { super(detail); this.status = status; this.detail = detail; } }

function makeHarness(path, kind) {
  const slots = [], params = { code: undefined }, identityListeners = [];
  const env = { identity: identity("A"), params, handlers: null, apiCalls: [], legal: [], ApiError: HarnessApiError };
  let cursor = 0, effects = [], changed = false;
  const same = (a, b) => a?.length === b?.length && a.every((x, i) => Object.is(x, b[i]));
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { value: typeof initial === "function" ? initial() : initial }; const slot = slots[i]; return [slot.value, v => { slot.value = typeof v === "function" ? v(slot.value) : v; changed = true; }]; },
    useRef(initial) { return slots[cursor++] ??= { current: initial }; },
    useEffect(fn, deps) { const i = cursor++; if (!same(slots[i]?.deps, deps)) effects.push(() => { slots[i]?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; }); },
    useCallback(fn, deps) { const i = cursor++; if (!same(slots[i]?.deps, deps)) slots[i] = { deps, fn }; return slots[i].fn; },
  };
  const primitive = new Proxy({}, { get: (_, name) => String(name) });
  const router = { replace: value => env.apiCalls.push(["replace", value]), push: value => env.apiCalls.push(["push", value]) };
  const api = {
    getLegalStatus: () => { const d = deferred(); env.legal.push(d); return d.promise; },
    acceptLegal: body => { env.acceptBody = body; return env.acceptDef?.promise ?? Promise.resolve(policy); },
    joinChapter: code => { env.joinedCode = code; return Promise.resolve(); },
  };
  class ApiError extends Error { constructor(status, detail) { super(detail); this.status = status; this.detail = detail; } }
  const stubs = {
    react: hooks,
    "react/jsx-runtime": {
      jsx: (type, props) => { if (type === "TextInput") env.textInputProps = props; return ({ type, props }); },
      jsxs: (type, props) => ({ type, props }), Fragment: "Fragment",
    },
    "expo-router": { useRouter: () => router, useLocalSearchParams: () => env.params, Redirect: "Redirect" },
    "react-native": primitive, "@expo/vector-icons": { Feather: "Feather" }, "@/components": primitive,
    "@/theme": { useTheme: () => ({}), spacing: primitive, typography: primitive, inputField: () => ({}) },
    "@/lib/legalLinks": { openLegalLink: async () => {}, PRIVACY_URL: "privacy", TERMS_URL: "terms" },
    "@/auth": { useSession: () => ({ status: "ready", refresh: async () => true }), withInviteCode: (route, code) => code ? `${route}?code=${code}` : route, hasFirebaseConfig: () => true, signOutUser: async () => {} },
    "@/auth/identity": {
      currentIdentity: () => env.identity,
      ownsIdentity: owner => owner === env.identity,
      onIdentityChanged: callback => { identityListeners.push(callback); return () => { const i = identityListeners.indexOf(callback); if (i >= 0) identityListeners.splice(i, 1); }; },
    },
    "@/api/auth": api, "@/api/chapters": api,
    "@/api/client": { ApiError },
  };
  const source = readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
  const marker = kind === "legal" ? "  return <Screen" : "  return (\n    <Screen";
  assert.notEqual(source.indexOf(marker), -1, `${path}: production render marker missing`);
  const capture = kind === "legal"
    ? "globalThis.__capture({submit, retry, setAge, setGuardian, setSubmitting, apiErrorCtor: ApiError, getState: () => ({status, age, guardian, submitting, error})});\n"
    : "globalThis.__capture({join, setCode, getState: () => ({code, joining, error})});\n";
  const instrumented = source.replace(marker, `${capture}${marker}`);
  const output = ts.transpileModule(instrumented, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const context = vm.createContext({ console, Promise, URL, setTimeout, clearTimeout, globalThis: { __capture: value => { env.handlers = value; } } });
  const module = { exports: {} };
  vm.runInContext(`(function(require,module,exports){${output}\n})`, context)(name => { assert.ok(name in stubs, `${path}: unstubbed ${name}`); return stubs[name]; }, module, module.exports);
  const component = module.exports.default;
  env.render = () => { cursor = 0; effects = []; changed = false; component(); for (const effect of effects) effect(); };
  env.settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); if (changed) env.render(); } };
  env.mount = async () => { env.render(); await env.settle(); };
  env.switchIdentity = async uid => { env.identity = identity(uid); for (const callback of [...identityListeners]) callback(env.identity); env.render(); await env.settle(); };
  return env;
}

async function main() {
  let failures = 0;
  const check = async (name, fn) => { try { await fn(); console.log(`PASS ${name}`); } catch (error) { failures++; console.log(`FAIL ${name}: ${error.message}`); } };

  await check("delayed invite param reaches production form and user edit survives", async () => {
    const e = makeHarness("app/(auth)/join-chapter.tsx", "join"); await e.mount();
    e.params.code = "LATE-CODE"; e.render(); await e.settle();
    assert.equal(e.handlers.getState().code, "LATE-CODE");
    e.textInputProps.onChangeText("USER-CODE"); await e.settle(); e.params.code = "STALE-CODE"; e.render(); await e.settle();
    assert.equal(e.handlers.getState().code, "USER-CODE");
  });

  await check("A to B policy transition fetches B and ignores A", async () => {
    const e = makeHarness("app/(auth)/legal-acceptance.tsx", "legal"); await e.mount(); assert.equal(e.legal.length, 1);
    const old = e.legal[0]; await e.switchIdentity("B"); assert.equal(e.legal.length, 2, "B policy fetch missing");
    old.resolve({ ...policy, material_change: false }); await e.settle(); assert.equal(e.handlers.getState().status, null);
    e.legal[1].resolve({ ...policy, material_change: true }); await e.settle(); assert.equal(e.handlers.getState().status.material_change, true);
  });

  await check("stale submit finally cannot clear B pending state", async () => {
    const e = makeHarness("app/(auth)/legal-acceptance.tsx", "legal"); await e.mount(); e.legal[0].resolve(policy); await e.settle();
    e.handlers.setAge(18); await e.settle();
    e.acceptDef = deferred(); const submit = e.handlers.submit(); await e.switchIdentity("B");
    e.handlers.setSubmitting(true); await e.settle(); e.acceptDef.resolve(policy); await submit; await e.settle();
    assert.equal(e.handlers.getState().submitting, true);
  });

  await check("policy 409 resets age and guardian before reload", async () => {
    const e = makeHarness("app/(auth)/legal-acceptance.tsx", "legal"); await e.mount(); e.legal[0].resolve(policy); await e.settle();
    e.handlers.setAge(17); e.handlers.setGuardian(true); await e.settle();
    e.acceptDef = deferred(); const submit = e.handlers.submit(); const apiError = Object.create(e.handlers.apiErrorCtor.prototype); apiError.status = 409; apiError.detail = "legal_policy_changed"; e.acceptDef.reject(apiError); await submit; await e.settle();
    const state = e.handlers.getState(); assert.equal(state.status, null); assert.equal(state.age, null); assert.equal(state.guardian, false);
  });

  if (failures) process.exitCode = 1; else console.log("verify:c438-legal-flow — all production hook checks passed");
}
await main();
