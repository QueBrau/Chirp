/** Execute the real event clients with deterministic hooks and an in-memory API.
 * This checks state/requests/element props, not native layout or React scheduling.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function compile(relativePath, require, tail = "") {
  const source = readFileSync(new URL(relativePath, import.meta.url), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const exports = {};
  runInNewContext(output + tail, { exports, require, console, Set, Map });
  return exports;
}

function harness(requireFactory, path, props = {}, exportName = "default", tail = "") {
  const slots = [];
  let cursor = 0;
  let effects = [];
  let changed = false;
  let tree;
  const same = (a, b) => a?.length === b?.length && a.every((x, i) => Object.is(x, b[i]));
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
      return [slots[i], (value) => {
        slots[i] = typeof value === "function" ? value(slots[i]) : value;
        changed = true;
      }];
    },
    useRef(value) {
      const i = cursor++;
      return slots[i] ??= { current: value };
    },
    useCallback(fn, deps) {
      const i = cursor++;
      if (!same(slots[i]?.deps, deps)) slots[i] = { deps, fn };
      return slots[i].fn;
    },
    useEffect(fn, deps) {
      const i = cursor++;
      if (!same(slots[i]?.deps, deps)) {
        slots[i]?.cleanup?.();
        slots[i] = { deps };
        effects.push(() => { slots[i].cleanup = fn(); });
      }
    },
  };
  const require = requireFactory(hooks);
  const screen = compile(path, (name) => {
    if (name === "react") return hooks;
    if (name === "react/jsx-runtime") return {
      jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }),
    };
    return require(name);
  }, tail)[exportName];
  const render = () => {
    cursor = 0;
    effects = [];
    changed = false;
    tree = screen(props);
    effects.forEach((run) => run());
  };
  return {
    render,
    async settle() {
      for (let i = 0; i < 12; i++) {
        await new Promise(setImmediate);
        if (changed) render();
      }
      return tree;
    },
  };
}

function nodes(tree) {
  if (tree == null || typeof tree !== "object") return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.props?.children)];
}

function content(node) {
  if (node == null) return "";
  if (typeof node !== "object") return String(node);
  if (Array.isArray(node)) return node.map(content).join(" ");
  return content(node.props?.children);
}

export async function runEventPaginationCases() {
  const ids = Array.from({ length: 500 }, (_, i) => `u${String(i).padStart(3, "0")}`);
  const viewer = { id: ids[499], campus_id: null };
  const membership = { chapter_id: "chapter", role: "member" };
  const event = { id: "event", chapter_id: "chapter", host_id: ids[0], canceled_at: null,
    visibility: "chapter", title: "Large event", starts_at: "2026-09-27T19:00:00Z" };
  const time = "2026-09-01T12:00:00Z";
  const replies = [...ids.slice(0, 249), viewer.id].map((user_id) => ({
    event_id: event.id, user_id, status: user_id === viewer.id ? "maybe" : "going", created_at: time,
  }));
  const invites = ids.map((invited_user_id) => ({ event_id: event.id, invited_user_id, created_at: time }));
  const calls = [];
  let failCounts = false;
  const request = async (path, { query = {} } = {}) => {
    calls.push({ path, query });
    if (path === "/events/event") return event;
    if (path.endsWith("/rsvps/mine")) return { status: "maybe" };
    if (path.endsWith("/rsvp-counts")) {
      if (failCounts) throw new Error("offline");
      return { going: 249, maybe: 1, cant: 0, invited_unanswered: 250 };
    }
    if (path.endsWith("/rsvps") || path.endsWith("/invites")) {
      const isReply = path.endsWith("/rsvps");
      const field = isReply ? "user_id" : "invited_user_id";
      let rows = isReply ? replies : invites;
      if (query.unanswered_only) rows = rows.filter((row) => !replies.some((r) => r.user_id === row.invited_user_id));
      if (query.after_user_id) {
        assert.equal(query.after, time, "client dropped the timestamp half of the cursor");
        rows = rows.filter((row) => row[field] > query.after_user_id);
      }
      return rows.slice(0, query.limit ?? 50);
    }
    if (path === "/me/event-invites-with-rsvps") {
      assert.equal(query.view, "actionable", "Home must filter before pagination");
      const rows = Array.from({ length: 63 }, (_, i) => ({ event: {
        ...event, id: `e${String(i).padStart(3, "0")}`,
      }, hosted_by: "Host chapter", my_rsvp_status: null }));
      if (query.before_id) assert.equal(query.before, event.starts_at);
      return rows.filter((row) => !query.before_id || row.event.id > query.before_id).slice(0, query.limit);
    }
    throw new Error(`unexpected API path: ${path}`);
  };
  const api = compile("../src/api/events.ts", (name) => {
    assert.equal(name, "./client");
    return { request };
  });
  const members = ids.map((user_id) => ({ user_id, display_name: user_id }));
  const primitive = new Proxy({}, { get: (_, name) => String(name) });
  const theme = { useTheme: () => ({ accentGradient: ["a", "b"] }),
    useAppearance: () => ({ campusColors: {} }), spacing: primitive, radii: primitive,
    light: {}, withAlpha: () => "color" };
  const resolve = (hooks) => (name) => {
    const mocks = {
      "react-native": primitive,
      "@expo/vector-icons": { Feather: "Feather" },
      "expo-router": { useLocalSearchParams: () => ({ id: "event" }), useRouter: () => ({}),
        useFocusEffect: (fn) => hooks.useEffect(fn, [fn]) },
      "react-native-safe-area-context": { useSafeAreaInsets: () => ({ top: 0, bottom: 0 }) },
      "@/api/events": api,
      "@/api/chapters": { listMembers: async () => members },
      "@/api/feed": {}, "@/api/moderation": {},
      "@/auth": { useSession: () => ({ user: viewer, memberships: [membership] }),
        useCampus: () => null, useCampusAccess: () => "unverified" },
      "@/org/OwnChapterProvider": { useOwnChapter: () => ({ sessionStatus: "ready", membership, chapterLoading: false }) },
      "@/components": primitive,
      "@/api/client": { ApiError: class extends Error {} },
      "@/lib/alert": { showApiError: () => {}, showAlert: () => {}, confirmAction: () => {} },
      "@/lib/dates": { eventWhen: () => "Soon", compactAge: () => "Now" },
      "@/lib/roster": { findMember: (rows, id) => rows.find((r) => r.user_id === id) },
      "@/theme": theme,
    };
    assert.ok(name in mocks, `unhandled component dependency ${name}`);
    return mocks[name];
  };

  const detail = harness(resolve, "../app/(tabs)/chapter/event/[id].tsx");
  detail.render();
  let tree = await detail.settle();
  const selected = nodes(tree).filter((n) => n.props?.accessibilityState?.selected);
  assert.equal(selected.length, 1);
  assert.match(content(selected[0]), /Maybe/, "own answer beyond row200 must stay selected");
  assert.ok(nodes(tree).some((n) => n.props?.caption === "250 unanswered · 200 shown"));
  const click = (label) => {
    const button = nodes(tree).find((n) => n.props?.label === label);
    assert.ok(button, `missing ${label}`);
    button.props.onPress();
  };
  click("Load more replies");
  tree = await detail.settle();
  click("Load more unanswered invitations");
  tree = await detail.settle();
  const guests = nodes(tree).filter((n) => n.type === "ListRow").map((n) => n.props.title);
  assert.equal(guests.length, 500);
  assert.equal(new Set(guests).size, 500, "every guest appears once across answered/unanswered groups");
  nodes(tree).find((n) => n.type === "ScrollView").props.refreshControl.props.onRefresh();
  tree = await detail.settle();
  assert.ok(nodes(tree).some((n) => n.props?.label === "Load more replies"));
  assert.match(content(nodes(tree).find((n) => n.props?.accessibilityState?.selected)), /Maybe/);
  failCounts = true;
  nodes(tree).find((n) => n.type === "ScrollView").props.refreshControl.props.onRefresh();
  tree = await detail.settle();
  assert.ok(nodes(tree).some((n) => n.props?.title === "Guest details unavailable"));
  assert.ok(nodes(tree).some((n) => n.props?.caption?.includes("total unavailable")));

  const home = harness(resolve, "../app/(tabs)/feed/index.tsx", { refreshVersion: 0 },
    "InvitesSection", "\nexports.InvitesSection = InvitesSection;");
  home.render();
  tree = await home.settle();
  assert.equal(nodes(tree).filter((n) => n.type === "Card").length, 50);
  click("Load more invitations");
  tree = await home.settle();
  assert.equal(nodes(tree).filter((n) => n.type === "Card").length, 63);
  assert.ok(!nodes(tree).some((n) => n.props?.label === "Load more invitations"));
  assert.ok(calls.some((c) => c.path.endsWith("/rsvps/mine")));
  console.log("  PASS  executed event detail: own RSVP, 500 guests, refresh, unknown totals");
  console.log("  PASS  executed Home: actionable query and continuation beyond 50");
}
