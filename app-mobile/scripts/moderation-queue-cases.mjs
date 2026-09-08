/** Execute the real moderation queue handlers (load/loadOlderReports/doRemove/doDismiss)
 * against a controllable API boundary and queued React state updaters. UI primitives are
 * stubs; the screen's own pagination/exhaustion logic is real, transpiled TSX, and
 * `@/lib/collectionPages` is compiled for real too, not mocked (c353).
 *
 * Mirrors the shape of collection-race-cases.mjs (single-consumer *-cases.mjs files are
 * the established split even with exactly one importer: event-pagination-cases.mjs,
 * payment-uncertainty-cases.mjs).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const time = n => new Date(Date.UTC(2026, 8, 1, 0, 0, n)).toISOString();
/** Odd n = chirp (removable via doRemove); even n = comment (dismiss-only via doDismiss) —
 * exercises both close paths across every test that drains a page. */
const report = n => ({
  id: `r${String(n).padStart(3, "0")}`,
  reporter_id: "reporter-A",
  target_type: n % 2 === 1 ? "chirp" : "comment",
  target_id: n % 2 === 1 ? `chirp-${n}` : null,
  forwarded_plaintext: null,
  reason: `Reason ${n}`,
  status: "open",
  created_at: time(n),
});
/** `count` rows counting down from `from`, newest-first — matches the server order. */
const page = (from, count) => Array.from({ length: count }, (_, i) => report(from - i));
const page1 = page(120, 50); // n=120..71 (the page that seeds the initial cursor)
const page2 = page(70, 50); // n=70..21
const page3 = page(20, 20); // n=20..1, SHORT — the page that flips `more` to false
const freshPage = page(1020, 50); // disjoint id space: proves a refresh replaced, not merged

/** Drain every row currently displayed, one at a time via the real close path, settling
 * after each so pagination/refill effects run exactly as they would on device. */
async function drainAll(e) {
  while (e.value().reports.length > 0) {
    const r = e.value().reports[0];
    if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
    else await e.value().doDismiss(r);
    await e.settle();
  }
}

function environment(options = {}) {
  const apiCalls = [], alerts = [];
  let cursor = 0, slots = [], effects = [], changed = false, alive = true, snapshot = null, tree;
  const same = (a, b) => a && b && a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = { state: typeof initial === "function" ? initial() : initial, updates: [] };
      const slot = slots[i];
      return [slot.state, update => { slot.updates.push(update); changed = true; }];
    },
    useRef(initial) { return slots[cursor++] ??= { current: initial }; },
    useCallback(fn, deps) {
      const i = cursor++;
      if (!same(slots[i]?.deps, deps)) slots[i] = { deps, value: fn };
      return slots[i].value;
    },
    useEffect(fn, deps) {
      const i = cursor++;
      if (!same(slots[i]?.deps, deps)) effects.push(() => {
        slots[i]?.cleanup?.(); slots[i] = { deps, cleanup: fn() };
      });
    },
  };
  const implementations = {
    listReports: async query => (query?.before !== undefined ? [] : page1),
    removeChirp: async () => ({}),
    resolveReport: async (id, status, reason) => ({ id, status, reason }),
  };
  const env = { apiCalls, alerts, api: implementations };
  const api = new Proxy({}, { get: (_, name) => (...args) => { apiCalls.push({ name, args }); return implementations[name](...args); } });
  const primitive = new Proxy({}, { get: (_, name) => String(name) });
  const context = vm.createContext({ console, Date, Set, Map, Symbol,
    __capture: value => { snapshot = value; },
  });
  const stubs = {
    react: hooks,
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "react-native": primitive,
    "@/api/moderation": api,
    "@/org/OwnChapterProvider": { useOwnChapter: () => ({
      membership: { chapter_id: "chapter-A", role: "secretary" },
      roleMeta: { eboard: ["secretary", "president", "treasurer", "vice_president", "historian"] },
    }) },
    "@/components": primitive,
    "@/lib/alert": { confirmAction: value => value.onConfirm(), showApiError: (...args) => alerts.push(args) },
    "@/theme": { useTheme: () => ({}), radii: {}, spacing: {} },
  };
  function compile(path, capture) {
    let source = options.sourceOverride?.(path) ?? readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
    if (capture) {
      const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      const fn = file.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "ModerationScreen");
      const lastReturn = fn.body.statements.filter(ts.isReturnStatement).at(-1);
      source = source.slice(0, lastReturn.getStart(file)) + `globalThis.__capture({${capture}});\n` + source.slice(lastReturn.getStart(file));
    }
    const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const module = { exports: {} };
    vm.runInContext(`(function(require,module,exports){${output}\n})`, context)(name => {
      if (name === "@/lib/collectionPages") return compile("src/lib/collectionPages.ts");
      assert.ok(name in stubs, `Unstubbed dependency: ${name}`); return stubs[name];
    }, module, module.exports);
    return module.exports;
  }
  const component = compile(
    "app/(tabs)/chapter/moderation.tsx",
    "reports,hasOlder,loadingOlder,allClear,needsRefill,doRemove,doDismiss,load,loadOlderReports",
  ).default;
  env.render = () => {
    if (!alive) return;
    changed = false;
    for (const slot of slots) if (slot?.updates) {
      for (const update of slot.updates.splice(0)) {
        if (typeof update === "function") {
          const next = update(slot.state); update(slot.state); // React can replay an updater.
          slot.state = next;
        } else slot.state = update;
      }
    }
    cursor = 0; effects = [];
    tree = component({});
    for (const effect of effects) effect();
  };
  env.settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); if (changed) env.render(); } };
  env.value = () => snapshot;
  env.close = () => { alive = false; for (const slot of slots) slot?.cleanup?.(); };
  env.mount = async () => { env.render(); await env.settle(); };
  return env;
}

async function run(cases, options) {
  let passed = 0;
  for (const [name, test] of cases) {
    if (options.only && !name.includes(options.only)) continue;
    const env = environment(options);
    try { await test(env); console.log(`PASS c353 ${name}`); passed++; }
    finally { env.close(); }
  }
  return passed;
}

export async function runModerationQueueCases(options = {}) {
  return run([
    ["initial load derives the stored cursor from row 50, not the display array", async e => {
      await e.mount();
      const request = e.apiCalls.filter(c => c.name === "listReports").at(0);
      assert.equal(request.args[0].status, "open");
      assert.equal(request.args[0].limit, 50);
      assert.equal(request.args[0].before, undefined);
      assert.equal(e.value().hasOlder, true);
      assert.equal(e.value().allClear, false);
      assert.equal(e.value().needsRefill, false); // 50 rows >= REFILL_THRESHOLD
    }],

    ["resolving the entire loaded page keeps the cursor at row 50 and defers all clear", async e => {
      await e.mount();
      const stalePage2 = deferred();
      e.api.listReports = query => (query.before !== undefined ? stalePage2.promise : Promise.resolve(page1));
      while (e.value().reports.length > 0) {
        const r = e.value().reports[0];
        assert.equal(e.value().allClear, false);
        if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
        else await e.value().doDismiss(r);
        await e.settle();
      }
      // Empty display array, but the page is not exhausted (page2 is still pending) —
      // this is exactly the state the old bug rendered "All clear" for.
      assert.equal(e.value().reports.length, 0);
      assert.equal(e.value().allClear, false);
      const refill = e.apiCalls.filter(c => c.name === "listReports" && c.args[0].before !== undefined);
      assert.equal(refill.length, 1);
      assert.equal(refill[0].args[0].before, time(71));
      assert.equal(refill[0].args[0].beforeId, "r071");
    }],

    ["closing a single row never moves the stored cursor", async e => {
      await e.mount();
      const newest = e.value().reports.find(r => r.id === "r120");
      await e.value().doDismiss(newest); // n=120 is even: comment, dismiss-only
      await e.settle();
      await e.value().loadOlderReports();
      await e.settle();
      const request = e.apiCalls.filter(c => c.name === "listReports").at(-1);
      assert.equal(request.args[0].before, time(71));
      assert.equal(request.args[0].beforeId, "r071");
    }],

    ["a short page marks the queue exhausted and blocks refill even under threshold pressure", async e => {
      const short = page(30, 30); // 30 < REPORT_PAGE_SIZE: exhausted from the first response
      e.api.listReports = async query => (query.before !== undefined ? [] : short);
      await e.mount();
      assert.equal(e.value().hasOlder, false);
      assert.equal(e.value().allClear, false);
      assert.equal(e.value().needsRefill, false);
      // Drain down to 5 — well under REFILL_THRESHOLD — and confirm no refill ever fires.
      while (e.value().reports.length > 5) {
        const r = e.value().reports[0];
        if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
        else await e.value().doDismiss(r);
        await e.settle();
      }
      assert.equal(e.value().needsRefill, false);
      assert.equal(e.apiCalls.filter(c => c.name === "listReports").length, 1);
    }],

    ["a full exhaustion sequence only clears the queue once every page is proven short", async e => {
      const page2Deferred = deferred(), page3Deferred = deferred();
      let beforeCalls = 0;
      e.api.listReports = query => {
        if (query.before === undefined) return Promise.resolve(page1);
        beforeCalls++;
        return beforeCalls === 1 ? page2Deferred.promise : page3Deferred.promise;
      };
      await e.mount();

      // Drain page1. The refill effect fires automatically once under threshold and
      // requests page2, but page2Deferred is still pending — the queue goes visibly
      // empty here without page2 having landed yet.
      while (e.value().reports.length > 0) {
        const r = e.value().reports[0];
        if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
        else await e.value().doDismiss(r);
        await e.settle();
      }
      assert.equal(e.value().hasOlder, true); // unresolved page1->page2 fetch hasn't landed
      assert.equal(e.value().allClear, false); // never true off the empty array alone

      page2Deferred.resolve(page2);
      await page2Deferred.promise; await e.settle();
      assert.equal(e.value().reports.length, 50);
      assert.equal(e.value().hasOlder, true);
      assert.equal(e.value().allClear, false);

      // Drain page2. The refill effect requests page3 and hangs on page3Deferred.
      while (e.value().reports.length > 0) {
        const r = e.value().reports[0];
        if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
        else await e.value().doDismiss(r);
        await e.settle();
      }
      const toPage3 = e.apiCalls.filter(c => c.name === "listReports").at(-1);
      assert.equal(toPage3.args[0].before, time(21));
      assert.equal(toPage3.args[0].beforeId, "r021");
      assert.equal(e.value().hasOlder, true); // page3 hasn't landed — still "more" from page2
      assert.equal(e.value().allClear, false);

      page3Deferred.resolve(page3);
      await page3Deferred.promise; await e.settle();
      assert.equal(e.value().reports.length, 20);
      assert.equal(e.value().hasOlder, false); // flips false only now that page3 (short) landed
      assert.equal(e.value().allClear, false); // rows still on screen

      // Drain page3. allClear must stay false until the very last row.
      while (e.value().reports.length > 1) {
        const r = e.value().reports[0];
        if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
        else await e.value().doDismiss(r);
        await e.settle();
        assert.equal(e.value().allClear, false);
      }
      const last = e.value().reports[0];
      if (last.target_type === "chirp") await e.value().doRemove(last, last.target_id);
      else await e.value().doDismiss(last);
      await e.settle();
      assert.equal(e.value().reports.length, 0);
      assert.equal(e.value().allClear, true);
    }],

    ["concurrent refill triggers collapse into one in-flight request", async e => {
      await e.mount();
      const page2Deferred = deferred();
      e.api.listReports = query => (query.before !== undefined ? page2Deferred.promise : Promise.resolve(page1));
      while (e.value().reports.length >= 10) {
        const r = e.value().reports[0];
        if (r.target_type === "chirp") await e.value().doRemove(r, r.target_id);
        else await e.value().doDismiss(r);
        await e.settle();
      }
      const before = () => e.apiCalls.filter(c => c.name === "listReports" && c.args[0].before !== undefined);
      assert.equal(before().length, 1); // the auto-refill effect already fired
      await e.value().loadOlderReports(); // manual tap races the still-pending fetch
      await e.settle();
      assert.equal(before().length, 1); // beginOlderPage's pending guard absorbed the second
      page2Deferred.resolve(page2);
      await page2Deferred.promise; await e.settle();
    }],

    ["a tombstoned report cannot reappear through a later page response", async e => {
      await e.mount();
      const boundary = e.value().reports.find(r => r.id === "r071"); // odd n: chirp, removable
      e.api.listReports = async query =>
        (query.before !== undefined ? [report(71), ...page2.slice(0, 5)] : page1);
      await e.value().doRemove(boundary, boundary.target_id);
      await e.settle();
      assert.equal(e.value().reports.some(r => r.id === "r071"), false);
      await e.value().loadOlderReports();
      await e.settle();
      assert.equal(e.value().reports.some(r => r.id === "r071"), false);
    }],

    ["pull-to-refresh replaces the page and discards a stale in-flight fetch", async e => {
      await e.mount();
      const stalePage2 = deferred();
      e.api.listReports = query => (query.before !== undefined ? stalePage2.promise : Promise.resolve(page1));
      const stalePending = e.value().loadOlderReports();
      await e.settle();
      assert.equal(e.value().loadingOlder, true); // fetch genuinely in flight

      e.api.listReports = async query => (query.before !== undefined ? stalePage2.promise : freshPage);
      await e.value().load();
      await e.settle();

      assert.equal(e.value().loadingOlder, false); // reset by refresh, not left stuck
      assert.equal(e.value().reports.length, 50);
      assert.equal(e.value().reports[0].id, "r1020"); // the fresh set, not page1
      assert.equal(e.value().reports.some(r => page1.some(p => p.id === r.id)), false);

      stalePage2.resolve(page2);
      await stalePending;
      await e.settle();
      // The late page2 response must not merge into the refreshed queue.
      assert.equal(e.value().reports.length, 50);
      assert.equal(e.value().reports.some(r => page2.some(p => p.id === r.id)), false);
    }],
  ], options);
}
