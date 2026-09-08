/** Execute secretary/comment components with deferred API boundaries and queued
 * React state updaters. UI primitives are stubs; component handlers are real TSX.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const time = n => new Date(Date.UTC(2026, 8, 1, 0, 0, n)).toISOString();
const meeting = n => ({ meeting: { id: `m${String(n).padStart(3, "0")}`, chapter_id: "chapter-A", title: `Meeting ${n}`, meeting_date: time(n), minutes_md: null }, attendance: [] });
const poll = n => ({ id: `p${String(n).padStart(3, "0")}`, chapter_id: "chapter-A", meeting_id: null, question: `Poll ${n}`, status: "open", created_by: "A", created_at: time(n), closed_at: null, options: [], total_votes: 1, my_option_id: "mine" });
const comment = n => ({ id: `c${String(n).padStart(3, "0")}`, post_id: "post-A", body: `Comment ${n}`, created_at: time(n), display_name: "Member", avatar_url: null });
const descending = factory => Array.from({ length: 50 }, (_, i) => factory(100 - i));
const commentPage = () => Array.from({ length: 50 }, (_, i) => comment(i + 51));

function environment(kind, options = {}) {
  const slots = [], apiCalls = [], listeners = new Set(), alerts = [], counts = [];
  let cursor = 0, effects = [], changed = false, phase = "idle", alive = true, snapshot = null, tree;
  const env = {
    owner: { uid: "A", generation: 1 }, chapter: "chapter-A", apiCalls, alerts, counts,
    props: { postId: "post-A", onClose() {}, onCountChange(count) {
      counts.push({ postId: env.props.postId, count });
      assert.equal(phase, "effect", "parent count callback must run after commit, outside state updaters/render");
    } },
  };
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
    myMemberships: async () => [{ chapter_id: env.chapter, role: "secretary" }],
    listMembers: async () => [], getAttendanceSummary: async () => ({ members: [] }),
    listMeetingsWithAttendance: async (_id, query) => query.before ? [] : descending(meeting),
    listPolls: async (_id, query) => query.before ? [] : descending(poll),
    listComments: async (_id, query) => query.before ? [] : commentPage(),
    createComment: async () => comment(101), createMeeting: async (_id, body) => ({ ...meeting(1).meeting, ...body }),
    updateMeeting: async (_chapter, id, body) => ({ ...meeting(Number(id.slice(1))).meeting, ...body }),
    putAttendance: async (_chapter, _id, body) => body.entries,
    deleteMeeting: async () => {}, createPoll: async () => poll(101),
    castVote: async () => poll(100), closePoll: async () => ({ ...poll(100), status: "closed" }),
    exportMeetingsCsv: async () => "csv",
  };
  env.api = implementations;
  const api = new Proxy({}, { get: (_, name) => (...args) => { apiCalls.push({ name, args }); return implementations[name](...args); } });
  const primitive = new Proxy({}, { get: (_, name) => String(name) });
  const context = vm.createContext({ console, Date, Set, Map, Symbol, AbortController, setTimeout, clearTimeout,
    __capture: value => { snapshot = value; },
  });
  const stubs = {
    react: hooks, "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "expo-router": { useFocusEffect: callback => hooks.useEffect(callback, [callback]) },
    "react-native": primitive, "@expo/vector-icons": { Feather: "Feather" },
    "react-native-safe-area-context": { useSafeAreaInsets: () => ({ top: 0, bottom: 0 }) },
    "@/api/chapters": api, "@/api/meetings": api, "@/api/polls": api, "@/api/feed": api,
    "@/auth": { useSession: () => ({ user: { id: env.owner.uid } }) },
    "@/auth/identity": { currentIdentity: () => env.owner, ownsIdentity: owner => owner === env.owner, requireIdentity: owner => { assert.equal(owner, env.owner); } },
    "@/api/client": { ApiError: class ApiError extends Error {} },
    "@/components": primitive, "./AppText": { AppText: "AppText" }, "./CharCounter": { CharCounter: "CharCounter" },
    "./EmptyState": { EmptyState: "EmptyState" }, "./GradientAvatar": { GradientAvatar: "GradientAvatar" },
    "@/lib/alert": { showApiError: (...args) => alerts.push(args), showAlert: (...args) => alerts.push(args), confirmAction: value => value.onConfirm() },
    "@/lib/dates": { calendarDay: value => new Date(value), compactAge: () => "now" },
    "@/lib/export": { shareCsv: async () => {} }, "@/org/semester": { currentSemesterWindow: () => ({ start: time(0), end: time(100) }) },
    "@/realtime/socket": { chirpSocket: { onStatus: () => () => {}, getStatus: () => "closed", onEvent: fn => { listeners.add(fn); return () => listeners.delete(fn); } }, isPollEvent: event => event.type === "poll" },
    "@/theme": { useTheme: () => ({}), light: {}, inputField: () => ({}), radii: {}, spacing: {}, withAlpha: () => "color" },
    "@/lib/contentLimits": { isOverLimit: () => false, MAX_COMMENT_BODY_LENGTH: 2000 },
  };
  function compile(path, capture) {
    let source = options.sourceOverride?.(path) ?? readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
    if (capture) {
      const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      const fn = file.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === (kind === "secretary" ? "SecretaryScreen" : "CommentsSheet"));
      const lastReturn = fn.body.statements.filter(ts.isReturnStatement).at(-1);
      source = source.slice(0, lastReturn.getStart(file)) + `globalThis.__capture({${capture}});\n` + source.slice(lastReturn.getStart(file));
    }
    const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const module = { exports: {} };
    vm.runInContext(`(function(require,module,exports){${output}\n})`, context)(name => {
      if (name === "@/lib/collectionPages") return compile("src/lib/collectionPages.ts");
      if (name === "@/api/operation") return compile("src/api/operation.ts");
      if (name === "../auth/identity") return stubs["@/auth/identity"];
      assert.ok(name in stubs, `Unstubbed dependency: ${name}`); return stubs[name];
    }, module, module.exports);
    return module.exports;
  }
  const component = kind === "secretary"
    ? compile("app/(tabs)/chapter/secretary.tsx", "items,polls,hasOlderMeetings,hasOlderPolls,loadingOlderMeetings,loadingOlderPolls,loadOlderMeetings,loadOlderPolls,handleCreateMeeting,setNewTitle,setNewDateText,saveMinutes,setMinutesDraft,removeMeeting,handleVote,handleClosePoll,handleCreatePoll,setNewQuestion,setNewOptions,setRetryKey,summary,changeWindow").default
    : compile("src/components/CommentsSheet.tsx", "comments,hasOlder,loadingOlder,loadState,draft,load,loadOlder,send,setDraft").CommentsSheet;
  env.render = () => {
    if (!alive) return;
    changed = false; phase = "updater";
    for (const slot of slots) if (slot?.updates) {
      for (const update of slot.updates.splice(0)) {
        if (typeof update === "function") {
          const next = update(slot.state); update(slot.state); // React can replay an updater.
          slot.state = next;
        } else slot.state = update;
      }
    }
    cursor = 0; effects = []; phase = "render";
    tree = component(env.props);
    phase = "effect"; for (const effect of effects) effect(); phase = "idle";
  };
  env.settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); if (changed) env.render(); } };
  env.value = () => snapshot;
  env.emit = event => { for (const fn of listeners) fn(event); };
  env.switchAccount = async (uid, chapter = env.chapter) => { env.owner = { uid, generation: env.owner.generation + 1 }; env.chapter = chapter; env.render(); await env.settle(); };
  env.switchPost = async postId => { env.props = { ...env.props, postId }; env.render(); await env.settle(); };
  env.close = () => { alive = false; for (const slot of slots) slot?.cleanup?.(); };
  env.mount = async () => { env.render(); await env.settle(); };
  return env;
}

async function run(kind, cases, options) {
  let passed = 0;
  for (const [name, test] of cases) {
    if (options.only && !name.includes(options.only)) continue;
    const env = environment(kind, options);
    try { await test(env); console.log(`PASS c358 ${name}`); passed++; }
    finally { env.close(); }
  }
  return passed;
}

export async function runSecretaryCollectionCases(options = {}) {
  return run("secretary", [
    ["historical meeting insertion cannot move the server archive cursor", async e => {
      await e.mount(); e.value().setNewTitle("Historical"); e.value().setNewDateText("2020-01-01"); await e.settle();
      await e.value().handleCreateMeeting(); await e.settle();
      await e.value().loadOlderMeetings(); await e.settle();
      const request = e.apiCalls.filter(call => call.name === "listMeetingsWithAttendance").at(-1);
      assert.equal(request.args[1].before, time(51)); assert.equal(request.args[1].beforeId, "m051");
    }],
    ["meeting page preserves edited minutes and deduplicates local historical creation", async e => {
      await e.mount(); const page = deferred(); e.api.listMeetingsWithAttendance = () => page.promise;
      const pending = e.value().loadOlderMeetings(); await e.settle();
      e.value().setMinutesDraft("new minutes"); await e.settle(); await e.value().saveMinutes(meeting(100).meeting); await e.settle();
      e.value().setNewTitle("Historical"); e.value().setNewDateText("2020-01-01"); await e.settle(); await e.value().handleCreateMeeting(); await e.settle();
      page.resolve([meeting(100), meeting(50), meeting(1)]); await pending; await e.settle();
      assert.equal(e.value().items.find(row => row.meeting.id === "m100").meeting.minutes_md, "new minutes");
      assert.equal(e.value().items.filter(row => row.meeting.id === "m001").length, 1);
      assert.equal(e.value().items.find(row => row.meeting.id === "m001").meeting.title, "Historical");
    }],
    ["live poll/page merge retains aggregates, own ballot and deletion tombstones", async e => {
      await e.mount(); const page = deferred(); e.api.listPolls = () => page.promise;
      const pending = e.value().loadOlderPolls(); await e.settle();
      e.emit({ type: "poll", action: "updated", chapter_id: e.chapter, poll_id: "p100", poll: { ...poll(100), total_votes: 9, my_option_id: undefined } });
      e.emit({ type: "poll", action: "updated", chapter_id: e.chapter, poll_id: "p049", poll: { ...poll(49), total_votes: 8, my_option_id: undefined } });
      e.emit({ type: "poll", action: "deleted", chapter_id: e.chapter, poll_id: "p050" }); await e.settle();
      page.resolve([poll(100), poll(50), poll(49)]); await pending; await e.settle();
      assert.equal(e.value().polls.find(row => row.id === "p100").total_votes, 9);
      assert.equal(e.value().polls.find(row => row.id === "p100").my_option_id, "mine");
      assert.equal(e.value().polls.find(row => row.id === "p049").total_votes, 8);
      assert.equal(e.value().polls.find(row => row.id === "p049").my_option_id, "mine");
      assert.equal(e.value().polls.some(row => row.id === "p050"), false);
      assert.equal(new Set(e.value().polls.map(row => row.id)).size, e.value().polls.length);
    }],
    ["double pagination taps reserve one request before render", async e => {
      await e.mount(); const page = deferred(); e.api.listPolls = () => page.promise;
      const first = e.value().loadOlderPolls(), second = e.value().loadOlderPolls();
      assert.equal(e.apiCalls.filter(call => call.name === "listPolls").length, 2);
      page.resolve([]); await Promise.all([first, second]); await e.settle();
    }],
    ["live historical poll does not move the independent server cursor", async e => {
      await e.mount(); e.emit({ type: "poll", action: "updated", chapter_id: e.chapter, poll_id: "p001", poll: poll(1) }); await e.settle();
      await e.value().loadOlderPolls(); await e.settle();
      const request = e.apiCalls.filter(call => call.name === "listPolls").at(-1);
      assert.equal(request.args[1].before, time(51)); assert.equal(request.args[1].beforeId, "p051");
    }],
    ["live opened poll arriving before create response retains its tally without duplication", async e => {
      await e.mount(); const created = deferred(); e.api.createPoll = () => created.promise;
      e.value().setNewQuestion("Question"); e.value().setNewOptions(["Yes", "No"]); await e.settle();
      const pending = e.value().handleCreatePoll();
      e.emit({ type: "poll", action: "opened", chapter_id: e.chapter, poll_id: "p101", poll: { ...poll(101), total_votes: 9 } }); await e.settle();
      e.api.listPolls = async () => [{ ...poll(101), total_votes: 9, my_option_id: null }, ...descending(poll).slice(0, 49)];
      created.resolve({ ...poll(101), total_votes: 0, my_option_id: null }); await pending; await e.settle();
      assert.equal(e.value().polls.filter(row => row.id === "p101").length, 1);
      assert.equal(e.value().polls.find(row => row.id === "p101").total_votes, 9);
      assert.equal(e.value().polls.find(row => row.id === "p101").my_option_id, null);
    }],
    ["local meeting with equal timestamp uses the server ID tie-break order", async e => {
      await e.mount(); e.api.createMeeting = async () => ({ ...meeting(1).meeting, meeting_date: time(100) });
      e.value().setNewTitle("Same time"); e.value().setNewDateText("2026-09-01"); await e.settle();
      await e.value().handleCreateMeeting(); await e.settle();
      assert.equal(e.value().items[0].meeting.id, "m100"); assert.equal(e.value().items[1].meeting.id, "m001");
    }],
    ["poll display preserves PostgreSQL microsecond ordering before ID tie-break", async e => {
      e.api.listPolls = async () => [
        { ...poll(1), created_at: "2026-09-01T00:00:00.000002+00:00" },
        { ...poll(2), created_at: "2026-09-01T00:00:00.000001+00:00" },
      ];
      await e.mount(); assert.deepEqual([...e.value().polls.map(row => row.id)], ["p001", "p002"]);
    }],
    ["deleted meeting cannot return through an older response", async e => {
      await e.mount(); const page = deferred(); e.api.listMeetingsWithAttendance = () => page.promise;
      const pending = e.value().loadOlderMeetings(); await e.value().removeMeeting(meeting(100).meeting); await e.settle();
      page.resolve([meeting(100), meeting(50)]); await pending; await e.settle();
      assert.equal(e.value().items.some(row => row.meeting.id === "m100"), false);
    }],
    ["account/chapter switch retires old pages, live callbacks and mutation completion", async e => {
      await e.mount(); const page = deferred(), write = deferred();
      const normal = e.api.listPolls; e.api.listPolls = () => page.promise; e.api.castVote = () => write.promise;
      const oldPage = e.value().loadOlderPolls(), oldVote = e.value().handleVote("p100", "other");
      e.api.listPolls = async () => [{ ...poll(10), chapter_id: "chapter-B", my_option_id: "B-vote" }];
      await e.switchAccount("B", "chapter-B");
      page.resolve([poll(100)]); write.resolve({ ...poll(100), my_option_id: "other" });
      await Promise.all([oldPage, oldVote]); await e.settle();
      assert.deepEqual([...e.value().polls.map(row => row.id)], ["p010"]);
      assert.equal(e.value().polls[0].my_option_id, "B-vote");
      e.emit({ type: "poll", action: "updated", chapter_id: "chapter-A", poll_id: "p100", poll: poll(100) }); await e.settle();
      assert.equal(e.value().polls.length, 1); assert.equal(e.alerts.length, 0); e.api.listPolls = normal;
    }],
    ["attendance window response cannot overwrite a newer selected window", async e => {
      await e.mount(); const old = deferred(); e.api.getAttendanceSummary = (_id, window) => Object.keys(window).length ? Promise.resolve({ members: [], tag: "semester" }) : old.promise;
      e.value().changeWindow("all"); await e.settle();
      e.api.getAttendanceSummary = async () => ({ members: [], tag: "semester" });
      e.value().changeWindow("semester"); await e.settle(); old.resolve({ members: [], tag: "all" }); await e.settle();
      assert.equal(e.value().summary.tag, "semester");
    }],
    ["old-account handler cannot launch a page under a replacement account in the same chapter", async e => {
      await e.mount(); const retired = e.value().loadOlderPolls;
      await e.switchAccount("B"); const count = e.apiCalls.filter(call => call.name === "listPolls").length;
      await retired(); await e.settle();
      assert.equal(e.apiCalls.filter(call => call.name === "listPolls").length, count);
    }],
    ["same-account dashboard retry retires a captured delete confirmation", async e => {
      await e.mount(); const oldRemove = e.value().removeMeeting;
      e.value().setRetryKey(key => key + 1); await e.settle();
      await oldRemove(meeting(100).meeting); await e.settle();
      assert.equal(e.apiCalls.filter(call => call.name === "deleteMeeting").length, 0);
    }],
    ...["create", "delete"].map(action => [`meeting ${action} completion refreshes the currently selected attendance window`, async e => {
      await e.mount(); const mutation = deferred(); e.api.getAttendanceSummary = async (_id, window) => ({ members: [], tag: Object.keys(window).length ? "semester" : "all" });
      let pending;
      if (action === "create") {
        e.api.createMeeting = () => mutation.promise;
        e.value().setNewTitle("Meeting"); e.value().setNewDateText("2026-09-01"); await e.settle();
        pending = e.value().handleCreateMeeting();
      } else { e.api.deleteMeeting = () => mutation.promise; pending = e.value().removeMeeting(meeting(100).meeting); }
      e.value().changeWindow("all"); await e.settle(); assert.equal(e.value().summary.tag, "all");
      mutation.resolve(meeting(1).meeting); await pending; await e.settle();
      assert.equal(e.value().summary.tag, "all");
      assert.equal(Object.keys(e.apiCalls.filter(call => call.name === "getAttendanceSummary").at(-1).args[1]).length, 0);
    }]),
    ["initial dashboard completion retains a window selected while pages were loading", async e => {
      const meetings = deferred(); e.api.listMeetingsWithAttendance = () => meetings.promise;
      e.api.getAttendanceSummary = async (_id, window) => ({ members: [], tag: Object.keys(window).length ? "semester" : "all" });
      await e.mount(); e.value().changeWindow("all"); await e.settle();
      meetings.resolve(descending(meeting)); await e.settle();
      assert.equal(e.value().summary.tag, "all");
    }],
  ], options);
}

export async function runCommentCollectionCases(options = {}) {
  return run("comments", [
    ...["send first", "page first"].map(order => [`concurrent comment send and older page retain both results (${order})`, async e => {
      await e.mount(); const page = deferred(), created = deferred(); e.api.listComments = () => page.promise; e.api.createComment = () => created.promise;
      e.value().setDraft("new comment"); await e.settle();
      const older = e.value().loadOlder(), send = e.value().send();
      if (order === "send first") { created.resolve(comment(101)); await send; await e.settle(); page.resolve([comment(50)]); }
      else { page.resolve([comment(50)]); await older; await e.settle(); created.resolve(comment(101)); }
      await Promise.all([older, send]); await e.settle();
      assert.equal(e.value().comments.length, 52); assert.equal(e.value().comments[0].id, "c050");
      assert.equal(e.value().comments.at(-1).id, "c101"); assert.equal(e.counts.at(-1).count, 52);
    }]),
    ["comment pages deduplicate overlap and notify totals after committed updates", async e => {
      await e.mount(); e.api.listComments = async () => [comment(50), comment(51)];
      await e.value().loadOlder(); await e.settle();
      assert.equal(e.value().comments.length, 51); assert.equal(e.counts.at(-1).count, 51);
    }],
    ["comment display preserves microseconds in oldest-first reading order", async e => {
      e.api.listComments = async () => [
        { ...comment(2), created_at: "2026-09-01T00:00:00.000001Z" },
        { ...comment(1), created_at: "2026-09-01T00:00:00.000002Z" },
      ];
      await e.mount(); assert.deepEqual([...e.value().comments.map(row => row.id)], ["c002", "c001"]);
    }],
    ["failed initial comments plus successful send cannot invent a complete count", async e => {
      e.api.listComments = async () => { throw new Error("offline"); }; await e.mount();
      e.value().setDraft("new comment"); await e.settle(); await e.value().send(); await e.settle();
      assert.equal(e.counts.length, 0);
      e.api.listComments = async () => commentPage(); await e.value().load(); await e.settle();
      assert.equal(e.counts.length, 0); assert.equal(e.value().comments.length, 51);
      e.api.listComments = async () => [comment(50)]; await e.value().loadOlder(); await e.settle();
      assert.equal(e.counts.at(-1).count, 52);
    }],
    ["post switch ignores old initial page and old send/count callbacks", async e => {
      const old = deferred(); e.api.listComments = () => old.promise; await e.mount();
      e.api.listComments = async () => [{ ...comment(1), post_id: "post-B" }];
      await e.switchPost("post-B"); old.resolve(commentPage()); await e.settle();
      assert.equal(e.value().comments.length, 1); assert.equal(e.value().comments[0].post_id, "post-B");
      assert.deepEqual(e.counts, [{ postId: "post-B", count: 1 }]);
    }],
    ["same-post account change retires old send without clearing the next draft", async e => {
      await e.mount(); const send = deferred(); e.api.createComment = () => send.promise;
      e.value().setDraft("A draft"); await e.settle(); const pending = e.value().send();
      e.api.listComments = async () => [comment(1)]; await e.switchAccount("B");
      e.value().setDraft("B draft"); await e.settle(); send.resolve(comment(101)); await pending; await e.settle();
      assert.equal(e.value().comments.length, 1); assert.equal(e.value().draft, "B draft");
    }],
    ["same-turn comment pagination taps reserve a single request", async e => {
      await e.mount(); const page = deferred(); e.api.listComments = () => page.promise;
      const first = e.value().loadOlder(), second = e.value().loadOlder();
      assert.equal(e.apiCalls.filter(call => call.name === "listComments").length, 2);
      page.resolve([]); await Promise.all([first, second]); await e.settle();
    }],
    ["retired comment handler cannot submit its old draft under the replacement account", async e => {
      await e.mount(); e.value().setDraft("A draft"); await e.settle(); const oldSend = e.value().send;
      await e.switchAccount("B"); await oldSend(); await e.settle();
      assert.equal(e.apiCalls.filter(call => call.name === "createComment").length, 0);
    }],
    ["unmount ignores late initial comments and parent notifications", async e => {
      const page = deferred(); e.api.listComments = () => page.promise; await e.mount(); e.close();
      page.resolve([comment(1)]); await e.settle(); assert.equal(e.counts.length, 0); assert.equal(e.alerts.length, 0);
    }],
    ["failed older comment page retries the same cursor without dropping loaded rows", async e => {
      await e.mount(); e.api.listComments = async () => { throw new Error("offline"); };
      await e.value().loadOlder(); await e.settle(); assert.equal(e.value().comments.length, 50);
      e.api.listComments = async () => [comment(50)]; await e.value().loadOlder(); await e.settle();
      const requests = e.apiCalls.filter(call => call.name === "listComments").slice(1);
      assert.equal(requests[0].args[1].beforeId, "c051"); assert.equal(requests[1].args[1].beforeId, "c051");
      assert.equal(e.value().comments.length, 51); assert.equal(e.counts.at(-1).count, 51);
    }],
  ], options);
}
