/** Execute the production account-data screen across auth-state seams. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../app/profile/account-data.tsx", import.meta.url), "utf8");
const identity = { uid: "runtime-user", generation: 1 };

function harness(status) {
  let cursor = 0;
  const slots = [];
  const apiCalls = [];
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
    listDataRequests: async () => { apiCalls.push("list"); return []; },
    createDataRequest: async () => { apiCalls.push("create"); return {}; },
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
    "@/lib/alert": { confirmAction: () => {}, showAlert: () => {}, showApiError: () => {} },
    "@/lib/export": { shareJson: async () => {} },
    "@/theme": { radii: primitive, spacing: primitive, useTheme: () => ({}) },
    "@/auth": { useSession: () => ({ status }) },
    "@/auth/identity": { currentIdentity: () => identity, onIdentityChanged: () => () => {}, ownsIdentity: () => true },
    "@/lib/legalLinks": { openLegalLink: async () => {} },
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
  return { rendered, apiCalls };
}

const signedOut = harness("signedOut");
assert.equal(signedOut.rendered.type, "Redirect");
assert.deepEqual(signedOut.apiCalls, []);
const suspended = harness("suspended");
await new Promise(resolve => setTimeout(resolve, 0));
assert.deepEqual(suspended.apiCalls, ["list"]);
console.log("verify:c436-account-data — auth guard and suspended access passed");
