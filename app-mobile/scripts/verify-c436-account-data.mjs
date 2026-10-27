/** Execute the production account-data screen across auth-state seams. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../app/profile/account-data.tsx", import.meta.url), "utf8");
const identity = { uid: "runtime-user", generation: 1 };

function harness(status, scenario = {}) {
  let cursor = 0;
  const slots = [];
  const apiCalls = [];
  const receipts = [];
  const confirmations = [];
  let owned = true;
  const hooks = {
    useState(initial) {
      const index = cursor++;
      slots[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [slots[index].value, value => { slots[index].value = typeof value === "function" ? value(slots[index].value) : value; }];
    },
    useRef(initial) { return slots[cursor++] ??= { current: initial }; },
    useEffect(effect) { effect(); cursor++; },
    useCallback(callback) { cursor++; return callback; },
  };
  const primitive = new Proxy({}, { get: (_, name) => String(name) });
  const api = {
    listDataRequests: async () => { apiCalls.push("list"); return scenario.items ?? []; },
    createDataRequest: async () => { apiCalls.push("create"); return scenario.created ?? {}; },
    downloadDataRequest: async () => { apiCalls.push("download"); return "{}"; },
  };
  const stubs = {
    react: hooks,
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }), Fragment: "Fragment" },
    "expo-router": { Redirect: "Redirect" },
    "@expo/vector-icons": { Feather: "Feather" },
    "react-native": primitive,
    "@/api/dataRequests": api,
    "@/api/client": { ApiError: class extends Error {} },
    "@/components": primitive,
    "@/lib/alert": { confirmAction: options => confirmations.push(options), showAlert: () => {}, showApiError: () => {} },
    "@/lib/export": { shareJson: async () => {} },
    "@/theme": { radii: primitive, spacing: primitive, useTheme: () => ({}) },
    "@/auth": { useSession: () => ({ status, user: scenario.user ?? null }) },
    "@/auth/identity": { currentIdentity: () => identity, onIdentityChanged: () => () => {}, ownsIdentity: () => owned },
    "@/lib/legalLinks": { openLegalLink: async () => {} },
    "@/crypto/deletionCompletionRuntime": { rememberDeletion: async (...args) => { receipts.push(args); return true; } },
  };
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const context = vm.createContext({ Promise, URL, console });
  const module = { exports: {} };
  vm.runInContext(`(function(require,module,exports){${output}\n})`, context)(name => {
    assert.ok(name in stubs, `unstubbed import ${name}`);
    return stubs[name];
  }, module, module.exports);
  cursor = 0;
  const rendered = module.exports.default();
  return { rendered, apiCalls, receipts, confirmations, switchAccount: () => { owned = false; } };
}

for (const [status, type, href] of [
  ["signedOut", "Redirect", "/sign-in"],
  ["unregistered", "Redirect", "/account-type"],
  ["loading", "LoadingScreen", undefined],
  ["recoverable", "Screen", undefined],
]) {
  const result = harness(status);
  assert.equal(result.rendered.type, type, status);
  if (href) assert.equal(result.rendered.props.href, href);
  assert.deepEqual(result.apiCalls, [], status);
}
for (const status of ["ready", "suspended", "legalRequired"]) {
  const result = harness(status);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(result.rendered.type, "Screen", status);
  assert.deepEqual(result.apiCalls, ["list"], status);
}
const user = { id: "00000000-0000-4000-8000-000000000001" };
const deletion = { id: "00000000-0000-4000-8000-000000000002", kind: "deletion", status: "blocked" };
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
const refreshed = harness("ready", { user, items: [deletion, { ...deletion, id: "export", kind: "export" }] });
await flush();
assert.deepEqual(refreshed.receipts, [[user.id, deletion.id, identity]], "status refresh prepares only deletion proof for its captured owner");

function findButton(node, label) {
  if (!node || typeof node !== "object") return null;
  if (node.type === "Button" && node.props.label === label) return node;
  const children = node.props?.children;
  for (const child of Array.isArray(children) ? children.flat(Infinity) : [children]) {
    const found = findButton(child, label); if (found) return found;
  }
  return null;
}
const submitted = harness("ready", { user, created: deletion });
findButton(submitted.rendered, "Request account deletion").props.onPress();
submitted.confirmations[0].onConfirm(); await flush();
assert.deepEqual(submitted.receipts, [[user.id, deletion.id, identity]], "successful intake stores a completion receipt");

const switched = harness("ready", { user, created: deletion });
findButton(switched.rendered, "Request account deletion").props.onPress();
switched.confirmations[0].onConfirm(); switched.switchAccount(); await flush();
assert.deepEqual(switched.receipts, [], "a stale intake response cannot attach a receipt to the replacement account");
console.log("verify:c436-account-data — 10 actual screen auth-state and deletion receipt cases passed");
