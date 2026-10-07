/** Execute the production organization-authority confirmation helper. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const sourcePath = new URL("../src/lib/confirmOrganizationAuthority.ts", import.meta.url);
const identity = { owner: { uid: "A", generation: 1 } };
identity.currentIdentity = () => identity.owner;
identity.ownsIdentity = (owner) => owner.uid === identity.owner.uid && owner.generation === identity.owner.generation;
const state = { confirmation: null, descriptions: [], actions: [] };
const module = { exports: {} };
const source = ts.transpileModule(readFileSync(sourcePath, "utf8"), {
  fileName: sourcePath.pathname,
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const requireModule = (name) => {
  if (name === "@/auth/identity") return identity;
  if (name === "./alert") return {
    confirmAction: (options) => {
      state.confirmation = options.onConfirm;
      state.descriptions.push(options.message);
    },
  };
  throw new Error(`unstubbed import ${name}`);
};
const context = vm.createContext({ Promise, console, __module: module, __require: requireModule });
vm.runInContext(`(function(require,module,exports){${source}\n})(__require,__module,__module.exports)`, context);
const confirmOrganizationAuthority = module.exports.confirmOrganizationAuthority;
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

async function check(name, callback) {
  state.confirmation = null;
  state.actions.length = 0;
  await callback();
  console.log(`PASS ${name}`);
}

await check("account switch while loading suppresses confirmation", async () => {
  identity.owner = { uid: "A", generation: 1 };
  const loaded = deferred();
  const pending = confirmOrganizationAuthority({
    load: () => loaded.promise, current: () => true, describe: (value) => value.name,
    act: async () => state.actions.push("act"),
  });
  identity.owner = { uid: "B", generation: 2 };
  loaded.resolve({ name: "A org" });
  await pending;
  assert.equal(state.confirmation, null);
});

await check("stale form predicate suppresses confirmation", async () => {
  identity.owner = { uid: "A", generation: 3 };
  await confirmOrganizationAuthority({
    load: async () => ({ name: "stale org" }), current: () => false, describe: (value) => value.name,
    act: async () => state.actions.push("act"),
  });
  assert.equal(state.confirmation, null);
});

await check("dialog cancellation performs no action", async () => {
  identity.owner = { uid: "A", generation: 4 };
  await confirmOrganizationAuthority({
    load: async () => ({ name: "org" }), current: () => true, describe: (value) => value.name,
    act: async () => state.actions.push("act"),
  });
  assert.equal(state.actions.length, 0);
});

await check("double confirmation performs one action", async () => {
  identity.owner = { uid: "A", generation: 5 };
  await confirmOrganizationAuthority({
    load: async () => ({ name: "org" }), current: () => true, describe: (value) => value.name,
    act: async () => state.actions.push("act"),
  });
  state.confirmation();
  state.confirmation();
  await Promise.resolve();
  assert.deepEqual(state.actions, ["act"]);
});

await check("account switch after dialog opens suppresses action", async () => {
  identity.owner = { uid: "A", generation: 6 };
  await confirmOrganizationAuthority({
    load: async () => ({ name: "org" }), current: () => true, describe: (value) => value.name,
    act: async () => state.actions.push("act"),
  });
  identity.owner = { uid: "B", generation: 7 };
  state.confirmation();
  await Promise.resolve();
  assert.equal(state.actions.length, 0);
});

console.log("c438 authority runtime evidence: 5 passed");
