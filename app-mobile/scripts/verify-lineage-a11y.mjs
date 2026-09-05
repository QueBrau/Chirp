/**
 * c334: the lineage screen's accessible pair list, proven by RENDERING it.
 *
 * Two directions, because the c332 defect passed every static gate:
 *   1. the a11y tree actually carries one named, focusable entry per edge, and
 *   2. the rendered SVG node count is UNCHANGED by hiding the canvas.
 *
 * Direction 2 is the one that matters. c332 put the a11y props on the <G>
 * inside TreeCanvas; it typechecked, it satisfied every source-level check, and
 * it DELETED rendered nodes. No amount of reading the diff catches that, so
 * this script executes the real TreeCanvas twice, bare and wrapped, and
 * compares the actual element counts in the output.
 *
 * The components are the real ones: TSX is transpiled and required at runtime,
 * never hand-copied. react-native resolves to react-native-web and
 * react-native-svg to its .web build (Metro's platform-extension resolution,
 * reimplemented here because the CJS resolver has none) so the tree renders to
 * real SVG markup through react-dom/server.
 *
 * Run: node scripts/verify-lineage-a11y.mjs
 */

import { createRequire } from "node:module";
import Module from "node:module";
import fs from "node:fs";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = path.join(ROOT, "src");
const require = createRequire(path.join(ROOT, "package.json"));

const failures = [];
const notes = [];
function check(ok, label, detail) {
  if (ok) notes.push(`  ok   ${label}`);
  else failures.push(`  FAIL ${label}${detail ? ` :: ${detail}` : ""}`);
}

/* ---------- module resolution: what Metro does, minus Metro ---------- */

const STUBS = new Map([
  // Font packages pull expo-font's native side at import; the components only
  // ever read the family-name constants, so a stub cannot mask a real defect.
  ["@expo-google-fonts/muli", path.join(ROOT, "node_modules", ".verify-font-stub.js")],
]);
fs.mkdirSync(path.dirname(STUBS.get("@expo-google-fonts/muli")), { recursive: true });
fs.writeFileSync(
  STUBS.get("@expo-google-fonts/muli"),
  "module.exports = new Proxy({ useFonts: () => [true] }, { get: (t, k) => (k in t ? t[k] : k) });\n",
);

const EXTS = ["", ".ts", ".tsx", ".js", "/index.ts", "/index.tsx", "/index.js"];
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (STUBS.has(request)) request = STUBS.get(request);
  if (request === "react-native") request = "react-native-web";
  if (request.startsWith("@/")) {
    const base = path.join(SRC, request.slice(2));
    for (const ext of EXTS) {
      const cand = base + ext;
      if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return cand;
    }
  }
  const resolved = origResolve.call(this, request, ...rest);
  // Metro prefers <name>.web.js for every module on the web platform. Without
  // this, packages fall back to their native specs and drag in react-native's
  // Flow source, which Node cannot parse.
  if (typeof resolved === "string" && resolved.endsWith(".js") && !resolved.endsWith(".web.js")) {
    const web = `${resolved.slice(0, -3)}.web.js`;
    if (fs.existsSync(web)) return web;
  }
  return resolved;
};

/* ---------- run the real TSX ---------- */

const ts = require("typescript");
for (const ext of [".ts", ".tsx"]) {
  require.extensions[ext] = (mod, filename) => {
    const out = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2020,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
      },
      fileName: filename,
    });
    mod._compile(out.outputText, filename);
  };
}

const listener = () => {};
globalThis.window = {
  innerWidth: 390,
  innerHeight: 844,
  devicePixelRatio: 2,
  addEventListener: listener,
  removeEventListener: listener,
  // PREVIEW_DARK flips the media query the theme reads, so the same render
  // path produces the dark palette without a second code path.
  matchMedia: (q) => ({
    matches: process.env.PREVIEW_DARK === "1" && String(q).includes("dark"),
    addListener: listener,
    removeListener: listener,
    addEventListener: listener,
    removeEventListener: listener,
  }),
};
globalThis.document = {
  createElement: () => ({ style: {} }),
  documentElement: { style: {} },
  head: { appendChild: listener },
  body: {},
  addEventListener: listener,
  removeEventListener: listener,
};
// Node 22+ ships a built-in `navigator` that is getter-only, so a plain
// assignment throws there while working fine on Node 20. defineProperty covers
// both; CI pins 20 but nobody should have to discover this locally.
Object.defineProperty(globalThis, "navigator", {
  value: { userAgent: "node", product: "" },
  configurable: true,
  writable: true,
});

const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { View } = require("react-native-web");
const { TreeCanvas } = require(path.join(SRC, "tree/TreeCanvas.tsx"));
const { LineagePairList } = require(path.join(SRC, "tree/LineagePairList.tsx"));

/* ---------- fixture: shapes the list has to survive ---------- */

const node = (id, name, extra = {}) => ({
  user_id: id,
  display_name: name,
  avatar_url: null,
  is_ghost: false,
  family_id: null,
  pledge_class: null,
  ...extra,
});
const edge = (id, big, little, extra = {}) => ({
  id,
  chapter_id: "c1",
  big_user_id: big,
  little_user_id: little,
  family_id: null,
  pledge_class: null,
  confirmed_by_little: true,
  created_by: "u1",
  created_at: "2026-01-01T00:00:00Z",
  ...extra,
});

const TREE = {
  families: [
    { id: "f1", chapter_id: "c1", name: "Ashby", color: "#0B2340" },
    { id: "f2", chapter_id: "c1", name: "Colvard", color: "#FFB71B" },
  ],
  nodes: [
    node("u1", "Marcus Hall"),
    node("u2", "Devon Price"),
    node("u3", "Andre Whitfield"),
    node("u4", "Tobias Reed", { is_ghost: true }),
    node("u5", "Cam Ellison"),
    node("u6", "Reggie Vance"),
  ],
  edges: [
    edge("e1", "u1", "u2", { family_id: "f1", pledge_class: "Spring 2024" }),
    edge("e2", "u4", "u1", { family_id: "f1" }),
    edge("e3", "u3", "u5", { family_id: "f2", confirmed_by_little: false }),
    // A family the tree payload never described, and a pair filed under none:
    // both must still reach the list or it renders shorter than the drawing.
    edge("e4", "u5", "u6", { family_id: "f-unknown" }),
    edge("e5", "u6", "u3"),
  ],
};

const render = (el) => renderToStaticMarkup(el);
const countTag = (html, tag) => (html.match(new RegExp(`<${tag}[\\s>]`, "g")) ?? []).length;

/* ---------- direction 2: the drawing must be untouched ---------- */

const bare = render(React.createElement(TreeCanvas, { tree: TREE, selectedUserId: null, onSelectUser: listener }));

const screenSrc = fs.readFileSync(path.join(ROOT, "app/(tabs)/chapter/tree.tsx"), "utf8");
const wrapMatch = screenSrc.match(/<View([^>]*?)>\s*<TreeCanvas/);
check(wrapMatch !== null, "the canvas is wrapped in a View that carries the hiding props");
const wrapProps = wrapMatch ? wrapMatch[1] : "";
check(
  /accessibilityElementsHidden/.test(wrapProps),
  "wrapper sets accessibilityElementsHidden (iOS)",
  wrapProps.trim(),
);
check(
  /importantForAccessibility=["']no-hide-descendants["']/.test(wrapProps),
  "wrapper sets importantForAccessibility=no-hide-descendants (Android)",
  wrapProps.trim(),
);
// The ban, asserted against the real component source: c332 put these on <G>.
const canvasSrc = fs.readFileSync(path.join(SRC, "tree/TreeCanvas.tsx"), "utf8");
check(
  !/accessibilityElementsHidden|importantForAccessibility|accessibilityRole/.test(canvasSrc),
  "no a11y props inside TreeCanvas (the banned prop-on-G route)",
);

// The wrapper is rendered with the props the SCREEN actually declares, parsed
// out of its source. Hardcoding them here would let this render pass while
// tree.tsx carried no props at all, which is the vacuous-test shape: a check
// that verifies the harness rather than the code.
const wrapperProps = {};
for (const m of wrapProps.matchAll(/([\w-]+)(?:=(?:"([^"]*)"|\{([^}]*)\}))?/g)) {
  const [, name, str, expr] = m;
  if (!name) continue;
  wrapperProps[name] = str !== undefined ? str : expr !== undefined ? expr.trim() !== "false" : true;
}
check(
  Object.keys(wrapperProps).length > 0,
  "wrapper props parsed off the real screen source",
  JSON.stringify(wrapperProps),
);

const wrapped = render(
  React.createElement(
    View,
    wrapperProps,
    React.createElement(TreeCanvas, { tree: TREE, selectedUserId: null, onSelectUser: listener }),
  ),
);

const TAGS = ["circle", "path", "g", "text", "svg"];
for (const tag of TAGS) {
  const before = countTag(bare, tag);
  const after = countTag(wrapped, tag);
  check(
    before === after,
    `<${tag}> count unchanged by hiding the canvas (${before})`,
    `bare=${before} wrapped=${after}`,
  );
}

// The bare-vs-wrapped comparison above is NOT the c332 detector, and believing
// it was is the trap this file nearly shipped with. Reproducing c332's route
// (a11y props on the <G>) drops <g> from 11 to 5 -- and both renders drop
// together, so every "unchanged" check stayed green while the drawing lost
// half its groups. A regression that hits both sides of a comparison is
// invisible to it.
//
// The real baseline is main's own TreeCanvas, rendered from git and compared
// against the working copy. A red here after a DELIBERATE canvas change is not
// a bug in this check: it means re-verify the drawing and re-baseline.
const BASELINE = path.join(SRC, "tree", "__baseline_TreeCanvas.tsx");
let baselineHtml = null;
try {
  const fromGit = execFileSync(
    "git",
    ["show", "origin/main:app-mobile/src/tree/TreeCanvas.tsx"],
    { cwd: ROOT, encoding: "utf8", maxBuffer: 8 << 20 },
  );
  // Written beside the original so its relative ./fonts and ./layout imports
  // resolve to the same modules the real component uses.
  fs.writeFileSync(BASELINE, fromGit);
  const { TreeCanvas: MainTreeCanvas } = require(BASELINE);
  baselineHtml = render(
    React.createElement(MainTreeCanvas, { tree: TREE, selectedUserId: null, onSelectUser: listener }),
  );
} catch (err) {
  notes.push(`  SKIP main-baseline node counts :: ${err.message.split("\n")[0]}`);
} finally {
  if (fs.existsSync(BASELINE)) fs.unlinkSync(BASELINE);
}

if (baselineHtml !== null) {
  for (const tag of TAGS) {
    const mainCount = countTag(baselineHtml, tag);
    const nowCount = countTag(bare, tag);
    check(
      mainCount === nowCount,
      `<${tag}> count matches main's canvas (${mainCount})`,
      `main=${mainCount} now=${nowCount}`,
    );
  }
}
check(countTag(bare, "circle") > 0, "the fixture actually draws nodes", `circles=${countTag(bare, "circle")}`);
check(
  !/tabindex="0"|tabIndex="0"/.test(wrapped),
  "nothing inside the hidden canvas is keyboard focusable (focusable content under aria-hidden is its own violation)",
  (wrapped.match(/tabindex="[^"]*"/g) ?? []).join(","),
);
check(/aria-hidden="true"/.test(wrapped), "the wrapper reaches the DOM as aria-hidden");

/* ---------- direction 1: the list carries the data ---------- */

const list = render(
  React.createElement(LineagePairList, { tree: TREE, selectedUserId: null, onSelectUser: listener }),
);

if (process.env.DUMP) { console.log("--- LIST ---"); console.log(list.slice(0, 1200)); console.log("--- WRAPPED HEAD ---"); console.log(wrapped.slice(0, 300)); }
const nameOf = (id) => TREE.nodes.find((n) => n.user_id === id).display_name;
// renderToStaticMarkup escapes; compare against the markup, not the intent.
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
for (const e of TREE.edges) {
  const label = `${nameOf(e.big_user_id)} is ${nameOf(e.little_user_id)}'s big`;
  check(list.includes(esc(label)), `a11y tree names the ${nameOf(e.big_user_id)} / ${nameOf(e.little_user_id)} pair`);
}
const buttons = (list.match(/role="button"/g) ?? []).length;
check(
  buttons >= TREE.edges.length,
  `every pair is a focusable button (${buttons} for ${TREE.edges.length} pairs)`,
  `buttons=${buttons}`,
);
check(/aria-label/.test(list), "rows expose aria-label rather than relying on child text order");
const rowHeights = [...list.matchAll(/min-height:(\d+)px/g)].map((m) => Number(m[1]));
check(
  rowHeights.length >= TREE.edges.length && rowHeights.every((h) => h >= 44),
  `every row clears the 44pt touch target (${rowHeights.length} rows, min ${Math.min(...rowHeights, Infinity)}px)`,
  `heights=${rowHeights.join(",")}`,
);
check(!/aria-hidden="true"/.test(list), "the list itself is NOT hidden from assistive tech");
// Reading the drawing's own data, not a second fetch: the names in the list
// come from tree.nodes, so an unrenderable pair is impossible by construction.
check(
  !/useEffect|fetch\(|getLineage/.test(fs.readFileSync(path.join(SRC, "tree/LineagePairList.tsx"), "utf8")),
  "the list fetches nothing of its own",
);
// Every edge reaches the list, including the unknown-family and no-family ones.
const rows = (list.match(/role="button"/g) ?? []).length;
check(rows === TREE.edges.length, `no pair is dropped by grouping (${rows}/${TREE.edges.length})`, `rows=${rows}`);

// Focusing a member who is a little in one pair and a big in another must
// announce exactly ONE selected row: the one whose press target they are.
// Visual affinity covers both rows; a11y selection does not.
const withSelection = render(
  React.createElement(LineagePairList, { tree: TREE, selectedUserId: "u5", onSelectUser: listener }),
);
// react-native-web drops accessibilityState for role="button", so aria-selected
// never reaches the DOM and this harness CANNOT render-verify it. Saying so
// rather than dropping the check: the a11y state is asserted at source, the
// visual half is asserted from the render, and the native announcement remains
// unverified here and belongs on the device pass.
const u5Pairs = TREE.edges.filter((e) => e.big_user_id === "u5" || e.little_user_id === "u5").length;
check(u5Pairs === 2, "fixture covers a member who is both a big and a little", `pairs=${u5Pairs}`);
const listSrc = fs.readFileSync(path.join(SRC, "tree/LineagePairList.tsx"), "utf8");
check(
  /accessibilityState=\{\{\s*selected:\s*isSelected\s*\}\}/.test(listSrc),
  "a11y selected state tracks the row's own press target, not mere involvement",
);
check(
  /const isSelected = selectedUserId === edge\.little_user_id;/.test(listSrc),
  "isSelected is defined as the press target",
);
// react-native-web expands borderColor into four longhands, so the neutral
// value is read off the UNSELECTED render rather than hardcoded: a palette
// change must not quietly turn this check green.
const borderOf = (html) =>
  [...html.matchAll(/<button[^>]*style="([^"]*)"/g)].map(
    (m) => (m[1].match(/border-top-color:([^;"]*)/) ?? [])[1] ?? "",
  );
const neutral = borderOf(list);
const highlighted = borderOf(withSelection).filter((c) => c !== neutral[0]);
check(
  new Set(neutral).size === 1,
  "no row is highlighted when nothing is selected",
  `distinct borders=${new Set(neutral).size}`,
);
check(
  highlighted.length === u5Pairs,
  `focusing a member marks every pair they appear in (${highlighted.length} of ${u5Pairs})`,
  `highlighted=${highlighted.length}`,
);
notes.push("  NOTE aria-selected is unobservable on web (RNW drops it for role=button); native announcement is a device check");

/* ---------- the screen still wires it up ---------- */

check(
  /<LineagePairList[\s\S]{0,200}?onSelectUser=\{setSelectedUserId\}/.test(screenSrc),
  "the list drives the same selection the canvas nodes did",
);
const listAt = screenSrc.indexOf("<LineagePairList");
const failAt = screenSrc.indexOf("treeFailed ? (");
check(
  failAt !== -1 && failAt < listAt,
  "the c333 failure branch still precedes the list, so a failed fetch is not rendered as an empty lineage",
);

/* ---------- optional visual preview ---------- */

// PREVIEW=<path> writes the list as a standalone page, styles included, for a
// look at the real thing without a device. renderToStaticMarkup alone omits
// react-native-web's stylesheet, which silently produced a wrong layout the
// first time; AppRegistry.getApplication() emits both halves.
//   PREVIEW=/tmp/list.html PREVIEW_DARK=1 node scripts/verify-lineage-a11y.mjs
if (process.env.PREVIEW) {
  const { AppRegistry } = require("react-native-web");
  const wantDark = process.env.PREVIEW_DARK === "1";
  const { Appearance } = require("react-native-web");
  const scheme = Appearance.getColorScheme();
  // Dark mode is NOT previewable here. react-native-web only consults
  // prefers-color-scheme when window.document.createElement exists, and
  // supplying that sends it down the real-DOM path, which needs jsdom (not a
  // dependency). Setting only the page background produced a light palette on
  // a dark canvas that read as a dark-mode screenshot. Refusing beats
  // shipping a convincing wrong picture; dark mode is a device check.
  if (wantDark && scheme !== "dark") {
    throw new Error(
      `PREVIEW_DARK cannot work without jsdom: the theme resolved "${scheme}". ` +
        "Dark mode belongs on the device pass, not this harness.",
    );
  }
  const bg = wantDark ? "#0C0D14" : "#F6F7FB";
  const Preview = () =>
    React.createElement(
      View,
      { style: { padding: 16, maxWidth: 390 } },
      React.createElement(LineagePairList, {
        tree: TREE,
        selectedUserId: process.env.PREVIEW_SELECT ?? null,
        onSelectUser: listener,
      }),
    );
  AppRegistry.registerComponent("Preview", () => Preview);
  const app = AppRegistry.getApplication("Preview");
  fs.writeFileSync(
    process.env.PREVIEW,
    `<meta name="viewport" content="width=390">${renderToStaticMarkup(app.getStyleElement())}` +
      `<body style="margin:0;background:${bg}">${renderToStaticMarkup(app.element)}</body>`,
  );
  notes.push(`  NOTE preview written to ${process.env.PREVIEW}`);
}

/* ---------- report ---------- */

console.log(notes.join("\n"));
if (failures.length > 0) {
  console.error(`\n${failures.length} FAILED:\n${failures.join("\n")}`);
  process.exit(1);
}
console.log(`\nALL PASS (${notes.length} checks)`);
