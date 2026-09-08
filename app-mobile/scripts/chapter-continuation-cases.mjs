/** Execute the chapter tab's Feed and Events segments with deferred API boundaries,
 * the same technique as collection-race-cases.mjs (real TSX component logic, stub UI
 * primitives), extended here to reach two components that are not module exports.
 *
 * OrgFeedSegment/OrgEventsSegment are local (non-exported) functions in
 * chapter/index.tsx, so `compile()` appends one extra line to the in-memory
 * transpiled copy - `module.exports.__target = <Name>;` - never to the real file on
 * disk, purely so this harness can reach a function collection-race-cases.mjs's
 * `.default`/`.CommentsSheet` lookup was never built to find.
 *
 * Board c359: the chapter tab's Feed and Events segments used to fetch only page
 * one and never continue, even though the backend already accepted a cursor on
 * both routes. This drives the real `load`/`loadOlder` handlers and asserts the
 * SHAPE of the next request (before/beforeId taken from the last row actually
 * held), not just that some cursor-looking identifier exists in the source.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const time = n => new Date(Date.UTC(2026, 8, 1, 0, 0, n)).toISOString();
const post = n => ({
  id: `post${String(n).padStart(3, "0")}`, chapter_id: "chapter-A", campus_id: "campus-A",
  author_id: "author-A", body: `Post ${n}`, media_urls: null, created_at: time(n),
  deleted_at: null, audience: "chapter", display_name: "Member", avatar_url: null,
  like_count: 0, comment_count: 0, liked_by_me: false,
});
const feedPage = (factory, count = 20) => Array.from({ length: count }, (_, i) => factory(100 - i));
const eventRow = n => ({
  event: { id: `ev${String(n).padStart(3, "0")}`, chapter_id: "chapter-A", host_id: "host-A",
    title: `Event ${n}`, description: "", location: "TBD", starts_at: time(n), ends_at: time(n),
    cover_seed: 0, visibility: "chapter", created_at: time(n) },
  counts: { going: 0, maybe: 0, cant: 0 }, going_preview: [], my_rsvp_status: null,
});

function environment(kind) {
  const slots = [], apiCalls = [], listeners = new Set(), alerts = [];
  let cursor = 0, effects = [], changed = false, phase = "idle", alive = true, snapshot = null, tree;
  const env = { apiCalls, alerts, chapter: "chapter-A", props: { chapterId: "chapter-A", orgName: "Org", refreshKey: 0 } };
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
    listPosts: async () => ({ posts: feedPage(post), activesOnlyHidden: false }),
    listEventsWithRsvps: async () => feedPage(eventRow),
    listMembers: async () => [], likePost: async () => {}, unlikePost: async () => {},
    createEvent: async () => eventRow(1).event, blockUser: async () => {}, createReport: async () => {},
  };
  env.api = implementations;
  const api = new Proxy({}, { get: (_, name) => (...args) => { apiCalls.push({ name, args }); return implementations[name](...args); } });
  const primitive = new Proxy({}, { get: (_, name) => String(name) });
  const context = vm.createContext({ console, Date, Set, Map, Symbol, __capture: value => { snapshot = value; } });
  const stubs = {
    react: hooks, "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "react-native": primitive, "@expo/vector-icons": { Feather: "Feather" },
    "react-native-qrcode-svg": { default: "QRCode" }, "expo-router": { useRouter: () => ({ push: () => {} }) },
    "@/api/chapters": api, "@/api/meetings": api, "@/api/polls": api, "@/api/feed": api,
    "@/api/events": api, "@/api/moderation": api,
    "@/auth": { useSession: () => ({ user: { id: "A" } }), inviteShareUrl: () => "https://example.edu/i/x", useCampus: () => ({ campus: null }) },
    "@/auth/identity": { currentIdentity: () => ({ uid: "A" }), ownsIdentity: () => true },
    "@/org/OwnChapterProvider": { useOwnChapter: () => ({ membership: null }) },
    "@/components": primitive,
    "@/lib/alert": { showApiError: (...args) => alerts.push(args), showAlert: (...args) => alerts.push(args), confirmAction: value => value.onConfirm() },
    "@/lib/dates": { compactAge: () => "now", eventWhen: () => "now" },
    "@/lib/roleTerms": { ROLE_LABELS: {}, roleLabel: () => "Member" },
    "@/lib/roster": { findMember: () => undefined },
    "@/theme": {
      useTheme: () => ({}), useAppearance: () => ({}), light: {}, cardShadow: () => ({}),
      radii: {}, spacing: {}, typography: {}, withAlpha: () => "color",
    },
  };
  function compile(path, capture, targetName) {
    let source = readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
    if (capture) {
      const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      const fn = file.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === targetName);
      assert.ok(fn, `target function ${targetName} not found in ${path}`);
      const lastReturn = fn.body.statements.filter(ts.isReturnStatement).at(-1);
      source = source.slice(0, lastReturn.getStart(file)) + `globalThis.__capture({${capture}});\n` + source.slice(lastReturn.getStart(file));
    }
    if (targetName) source += `\nmodule.exports.__target = ${targetName};\n`;
    const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const module = { exports: {} };
    vm.runInContext(`(function(require,module,exports){${output}\n})`, context)(name => {
      if (name === "@/lib/collectionPages") return compile("src/lib/collectionPages.ts");
      assert.ok(name in stubs, `Unstubbed dependency: ${name}`); return stubs[name];
    }, module, module.exports);
    return module.exports;
  }
  const capture = kind === "feed"
    ? "items,hasOlder,loadingOlder,loadOlder,load"
    : "events,hasOlder,loadingOlder,loadOlder,reload";
  const targetName = kind === "feed" ? "OrgFeedSegment" : "OrgEventsSegment";
  const component = compile("app/(tabs)/chapter/index.tsx", capture, targetName).__target;
  env.render = () => {
    if (!alive) return;
    changed = false; phase = "updater";
    for (const slot of slots) if (slot?.updates) {
      for (const update of slot.updates.splice(0)) {
        if (typeof update === "function") { const next = update(slot.state); slot.state = next; }
        else slot.state = update;
      }
    }
    cursor = 0; effects = []; phase = "render";
    tree = component(env.props);
    phase = "effect"; for (const effect of effects) effect(); phase = "idle";
  };
  env.settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); if (changed) env.render(); } };
  env.value = () => snapshot;
  env.close = () => { alive = false; for (const slot of slots) slot?.cleanup?.(); };
  env.mount = async () => { env.render(); await env.settle(); };
  return env;
}

async function run(kind, cases) {
  let passed = 0;
  for (const [name, test] of cases) {
    const env = environment(kind);
    try { await test(env); console.log(`PASS c359 ${name}`); passed++; }
    finally { env.close(); }
  }
  return passed;
}

export async function runChapterFeedContinuationCases() {
  return run("feed", [
    ["Feed segment's load-older request carries the last held post's cursor", async e => {
      await e.mount();
      assert.equal(e.value().items.length, 20, "a full first page must report more behind it");
      assert.equal(e.value().hasOlder, true);
      const lastOfFirstPage = e.value().items.at(-1).post;
      const olderPage = Array.from({ length: 5 }, (_, i) => post(60 - i));
      e.api.listPosts = async () => ({ posts: olderPage, activesOnlyHidden: false });
      await e.value().loadOlder(); await e.settle();
      const request = e.apiCalls.filter(call => call.name === "listPosts").at(-1);
      assert.equal(request.args[1].before, lastOfFirstPage.created_at);
      assert.equal(request.args[1].before_id, lastOfFirstPage.id);
      assert.equal(e.value().items.at(-1).post.id, olderPage.at(-1).id, "the older page must actually be appended");
    }],
    ["Feed segment stops offering more once a short page arrives", async e => {
      await e.mount();
      e.api.listPosts = async () => ({ posts: Array.from({ length: 3 }, (_, i) => post(60 - i)), activesOnlyHidden: false });
      await e.value().loadOlder(); await e.settle();
      assert.equal(e.value().hasOlder, false, "a short (final) page must clear hasOlder");
    }],
    ["Feed segment appends without duplicating an overlapping row", async e => {
      await e.mount();
      const held = e.value().items.length;
      const overlap = e.value().items.at(-1).post;
      e.api.listPosts = async () => ({ posts: [overlap, post(20)], activesOnlyHidden: false });
      await e.value().loadOlder(); await e.settle();
      assert.equal(e.value().items.length, held + 1, "the overlapping row must be merged, not duplicated");
      assert.equal(e.value().items.filter(item => item.post.id === overlap.id).length, 1);
    }],
  ]);
}

export async function runChapterEventsContinuationCases() {
  return run("events", [
    ["Events segment's load-older request carries the last held event's cursor", async e => {
      await e.mount();
      assert.equal(e.value().events.length, 20);
      assert.equal(e.value().hasOlder, true);
      const lastOfFirstPage = e.value().events.at(-1).event;
      const olderPage = Array.from({ length: 5 }, (_, i) => eventRow(60 - i));
      e.api.listEventsWithRsvps = async () => olderPage;
      await e.value().loadOlder(); await e.settle();
      const request = e.apiCalls.filter(call => call.name === "listEventsWithRsvps").at(-1);
      assert.equal(request.args[1].before, lastOfFirstPage.starts_at);
      assert.equal(request.args[1].beforeId, lastOfFirstPage.id);
      assert.equal(e.value().events.at(-1).event.id, olderPage.at(-1).event.id, "the older page must actually be appended");
    }],
    ["Events segment stops offering more once a short page arrives", async e => {
      await e.mount();
      e.api.listEventsWithRsvps = async () => Array.from({ length: 2 }, (_, i) => eventRow(60 - i));
      await e.value().loadOlder(); await e.settle();
      assert.equal(e.value().hasOlder, false);
    }],
  ]);
}
