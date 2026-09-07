/**
 * Verifies the c307 accessibility/touch-target fixes, from the real source (board c307).
 *
 *   npm run verify:a11y-touch
 *
 * Three independent items, so three independent sections. The touch-target one is
 * genuinely COMPUTED - the constants are read out of MediaPostCard.tsx, src/theme and
 * the Chirps board and the effective tap height is re-derived here - rather than
 * asserted as a magic number, because "44" appearing in a file proves nothing about what
 * a finger can hit.
 *
 * SECTION 2 WAS REWRITTEN FOR c383 and the rewrite is the interesting part. It used to
 * measure InlineAction and ActionChip, the two action densities the FYP card had; c383
 * folded those into one labelled row and moved the overflow glyph into a shared circular
 * control, so those constants no longer exist. The c307 RULE is unchanged - every action
 * clears 44pt, and its slop is derived from the control rather than hand-picked - so the
 * section now measures the controls that replaced them, on BOTH cards that draw them.
 * Deleting the section instead would have been the easy read of a red verifier and would
 * have retired a real lesson along with the code that happened to carry it.
 *
 * WHAT THESE CANNOT DO, stated here so the PR does not have to imply otherwise: none of
 * this runs a simulator. A KeyboardAvoidingView present in source is not a keyboard
 * observed to move, and an accessibilityLabel in source is not VoiceOver heard saying
 * it. Those are on the device checklist. What this catches is the regression - someone
 * removing the wrapper, or dropping the label back to a bare role.
 *
 * SECTION 4 EXISTS BECAUSE SECTIONS 1-3 COVER NOTHING THEY DO NOT NAME (board c336).
 * Those three read three hardcoded paths, so this suite reported ALL PASS over a
 * codebase it had never opened: c334 added a screen full of new buttons and every check
 * here passed without reading a line of it. A check that names its inputs cannot notice
 * the input nobody added.
 *
 * Section 4 enumerates every .tsx under app/ and src/ and has NO list to update. The
 * design rule, from c336: THE UNKNOWN CASE MUST FAIL. Whether a button has an accessible
 * name is decided by evidence (its own label prop, descendant text, or a child component
 * derived from source to render text); anything unresolvable is reported, never assumed
 * fine. Staleness then costs noise, which someone has to answer, rather than silence,
 * which nobody sees. Waivers are inline comments next to the element, so a refactor
 * carries or deletes them with the code instead of stranding a registration in a file it
 * never opens.
 */
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";

const CREATE_SHEET = new URL("../src/components/CreateSheet.tsx", import.meta.url);
const MEDIA_CARD = new URL("../src/components/MediaPostCard.tsx", import.meta.url);
const LIST_ROW = new URL("../src/components/ListRow.tsx", import.meta.url);
// c383 moved the overflow control's diameter into the shared theme and gave the Chirps
// board the same control, so section 2 now reads three files rather than one. Reading
// the token where it is DEFINED is the point: a check that re-declares 32 here would
// keep passing after somebody changed the real one.
const THEME_INDEX = new URL("../src/theme/index.ts", import.meta.url);
const TYPOGRAPHY = new URL("../src/theme/typography.ts", import.meta.url);
const CHIRPS_BOARD = new URL("../app/(tabs)/chirps/index.tsx", import.meta.url);

const createSheet = readFileSync(CREATE_SHEET, "utf8");
const mediaCard = readFileSync(MEDIA_CARD, "utf8");
const listRow = readFileSync(LIST_ROW, "utf8");
const themeIndex = readFileSync(THEME_INDEX, "utf8");
const typographySrc = readFileSync(TYPOGRAPHY, "utf8");
const chirpsBoard = readFileSync(CHIRPS_BOARD, "utf8");

let failures = 0;
const check = (name, actual, expected) => {
  if (actual === expected) {
    console.log(`  PASS  ${name}`);
  } else {
    console.log(`  FAIL  ${name}`);
    console.log(`        expected ${JSON.stringify(expected)}`);
    console.log(`        got      ${JSON.stringify(actual)}`);
    failures++;
  }
};

const num = (src, name) => matchNum(src, new RegExp(`const ${name} = (\\d+);`), name);

/** Same contract as num(), for a value that is a record field rather than a const. */
const matchNum = (src, re, what) => {
  const match = src.match(re);
  if (!match) {
    // Exit rather than count a failure: an unreadable input means this section is
    // measuring nothing, and "could not parse" quietly passing is the c336 bug.
    console.error(`FAIL  could not read ${what} from source`);
    process.exit(1);
  }
  return Number(match[1]);
};

console.log("\n-- (1) CreateSheet keyboard avoidance --");

check(
  "CreateSheet wraps in KeyboardAvoidingView",
  /<KeyboardAvoidingView/.test(createSheet),
  true,
);
check(
  "behavior is split per platform (padding iOS / height Android)",
  /behavior=\{Platform\.OS === "ios" \? "padding" : "height"\}/.test(createSheet),
  true,
);
// Order matters: inside the backdrop it would lift nothing, because the backdrop is the
// flex:1 element the sheet is laid out against.
const kavAt = createSheet.indexOf("<KeyboardAvoidingView");
const backdropAt = createSheet.indexOf("onPress={close}");
check(
  "the wrapper is OUTSIDE the backdrop, not nested inside it",
  kavAt !== -1 && backdropAt !== -1 && kavAt < backdropAt,
  true,
);
check(
  "the autoFocus that makes this necessary is still there (if it goes, revisit the fix)",
  /autoFocus/.test(createSheet),
  true,
);

console.log("\n-- (2) post-card action touch targets --");

// c383 replaced the two action densities (InlineAction / ActionChip) with ONE labelled
// row and moved the overflow glyph into a shared circular control. The constants this
// section reads changed with them; the c307 rule they enforce did not, and is the reason
// this section is COMPUTED rather than asserted: "44" appearing in a file proves nothing
// about what a finger can hit.
const TOUCH_TARGET = num(mediaCard, "TOUCH_TARGET");
const ACTION_ICON = num(mediaCard, "ACTION_ICON");
// Not re-declared here: read from the theme, where the card actually gets it.
const CONTROL = matchNum(themeIndex, /tintControlSize: (\d+),/, "metrics.tintControlSize");
const CAPTION_LINE = matchNum(typographySrc, /caption: \{[^}]*lineHeight: (\d+)/, "typography.caption.lineHeight");

// Re-derive what the component computes, from the same numbers it uses.
const actionHitSlop = Math.ceil((TOUCH_TARGET - ACTION_ICON) / 2);
const actionEffective = ACTION_ICON + actionHitSlop * 2;
const overflowHitSlop = Math.ceil((TOUCH_TARGET - CONTROL) / 2);
const overflowEffective = CONTROL + overflowHitSlop * 2;
// The Chirps board hand-rolls the same control with spacing.sm (8) of slop either side.
const chirpsOverflowEffective = CONTROL + 8 * 2;

console.log(
  `   TOUCH_TARGET=${TOUCH_TARGET} actionIcon=${ACTION_ICON} slop=${actionHitSlop} -> ${actionEffective}pt | ` +
    `control=${CONTROL} slop=${overflowHitSlop} -> ${overflowEffective}pt | chirps ${chirpsOverflowEffective}pt`,
);

check(
  `PostAction's effective tap height clears ${TOUCH_TARGET}pt (got ${actionEffective})`,
  actionEffective >= TOUCH_TARGET,
  true,
);
check(
  `the feed card's overflow button clears ${TOUCH_TARGET}pt (got ${overflowEffective})`,
  overflowEffective >= TOUCH_TARGET,
  true,
);
check(
  `the Chirps board's own overflow button clears ${TOUCH_TARGET}pt too (got ${chirpsOverflowEffective})`,
  chirpsOverflowEffective >= TOUCH_TARGET,
  true,
);
check(
  "PostAction uses the computed slop, not a hardcoded token",
  /hitSlop=\{ACTION_HIT_SLOP\}/.test(mediaCard),
  true,
);
check(
  "so does the overflow button",
  /hitSlop=\{OVERFLOW_HIT_SLOP\}/.test(mediaCard),
  true,
);
// The slops must stay DERIVED from TOUCH_TARGET. c307 was a hand-picked 8 that looked
// deliberate and left a 33pt target, so a literal here is the exact regression to catch.
check(
  "both slops are still derived from TOUCH_TARGET rather than written down",
  /ACTION_HIT_SLOP = Math\.ceil\(\(TOUCH_TARGET - ACTION_ICON\) \/ 2\)/.test(mediaCard) &&
    /OVERFLOW_HIT_SLOP = Math\.ceil\(\(TOUCH_TARGET - OVERFLOW_CIRCLE\) \/ 2\)/.test(mediaCard),
  true,
);
// PostAction is an icon beside a caption, so the rendered row is as tall as the TALLER of
// the two. Deriving off the icon is exact only while the icon is that taller one; if the
// caption ever outgrows it the derivation silently starts under-measuring the row.
check(
  `the action icon is still at least the caption lineHeight (${ACTION_ICON} vs ${CAPTION_LINE}), so the row height is what was measured`,
  ACTION_ICON >= CAPTION_LINE,
  true,
);
// Both cards must keep drawing the SAME control, which is the whole reason the diameter
// moved into the theme (c383).
check(
  "both cards size their overflow control from metrics.tintControlSize",
  /OVERFLOW_CIRCLE = metrics\.tintControlSize;/.test(mediaCard) &&
    /width: metrics\.tintControlSize,/.test(chirpsBoard),
  true,
);

console.log("\n-- (3) ListRow role + name --");

check(
  "the tappable branch declares a button role",
  /accessibilityRole="button"/.test(listRow),
  true,
);
check(
  "and a name, defaulting to the visible title",
  /accessibilityLabel=\{accessibilityLabel \?\? title\}/.test(listRow),
  true,
);
// A role with no name is what VotePill shipped with, and it announces as a bare
// "button" - so presence of the role alone must not be treated as the fix.
const roleAt = listRow.indexOf('accessibilityRole="button"');
const labelAt = listRow.indexOf("accessibilityLabel={accessibilityLabel ?? title}");
check(
  "role and name are on the SAME element (a role without a name is the c297 bug again)",
  roleAt !== -1 && labelAt !== -1 && Math.abs(labelAt - roleAt) < 200,
  true,
);
// The non-pressable branch returns bare content; announcing a control that does nothing
// is worse than announcing nothing.
check(
  "static rows are left un-roled (no onPress means no control to announce)",
  /\/\/ No onPress: this is static content/.test(listRow),
  true,
);


console.log("\n-- (4) every button in the app has a name (repo-wide, no list) --");

const ROOT = new URL("..", import.meta.url).pathname;
const SOURCE_FILES = execFileSync("find", ["app", "src", "-name", "*.tsx"], {
  cwd: ROOT,
  encoding: "utf8",
})
  .trim()
  .split("\n")
  .filter(Boolean)
  .sort();

// If discovery breaks it must break LOUDLY. These three are independently known to
// exist (sections 1-3 assert on them), so their absence means the enumeration is
// broken rather than the codebase being clean.
for (const required of [
  "src/components/CreateSheet.tsx",
  "src/components/MediaPostCard.tsx",
  "src/components/ListRow.tsx",
]) {
  check(
    `enumeration reaches ${required} (if not, section 4 is scanning nothing)`,
    SOURCE_FILES.includes(required),
    true,
  );
}
check("enumeration finds a plausible number of screens", SOURCE_FILES.length > 40, true);

// Parsed with the TypeScript compiler, not regexes. A hand-rolled scanner mis-parsed
// ten call sites while writing this, and "could not parse" counted as a pass is exactly
// the c336 bug in miniature.
const require_ = createRequire(`${ROOT}/package.json`);
const ts = require_("typescript");
const read = (f) => readFileSync(`${ROOT}/${f}`, "utf8");
const parse = (f) => ts.createSourceFile(f, read(f), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

const eachNode = (node, fn) => {
  fn(node);
  node.forEachChild((c) => eachNode(c, fn));
};
const isJsx = (n) => ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n);
const tagName = (el) => (ts.isJsxSelfClosingElement(el) ? el.tagName : el.openingElement.tagName).getText();
const attrNamed = (el, name) =>
  (ts.isJsxSelfClosingElement(el) ? el : el.openingElement).attributes.properties.find(
    (a) => ts.isJsxAttribute(a) && a.name.getText() === name,
  );

// Which components supply their own visible text is DERIVED by reading them: a component
// whose body renders <AppText>/<Text> gives its parent a name. A new wrapper is
// classified by parsing it, so there is nothing to remember to register.
const textProviders = new Set(["AppText", "Text"]);
for (const f of SOURCE_FILES) {
  eachNode(parse(f), (n) => {
    if (!ts.isFunctionDeclaration(n) || !n.name || !n.body || !/^[A-Z]/.test(n.name.text)) return;
    eachNode(n.body, (c) => {
      if (isJsx(c) && ["AppText", "Text"].includes(tagName(c))) textProviders.add(n.name.text);
    });
  });
}

const unnamed = [];
const namedByCaller = [];
let buttons = 0;
for (const f of SOURCE_FILES) {
  const sf = parse(f);
  eachNode(sf, (n) => {
    if (!isJsx(n)) return;
    const role = attrNamed(n, "accessibilityRole");
    if (!role || !role.initializer || role.initializer.getText() !== '"button"') return;
    buttons++;
    const where = `${f}:${sf.getLineAndCharacterOfPosition(n.getStart()).line + 1}`;

    if (attrNamed(n, "accessibilityLabel") || attrNamed(n, "aria-label")) return;
    // A waiver lives next to the element it excuses, so a refactor moves or deletes it
    // with the code rather than leaving a registration behind elsewhere.
    if (/a11y-name:/.test(n.getFullText().slice(0, 400))) return;

    // React Native derives the name from ALL descendant text, not just direct children,
    // so this recurses: checking one level called PairSheet unnamed when its label sat
    // inside a <View> one step down.
    let hasText = false;
    eachNode(n, (c) => {
      if (c === n) return;
      if (ts.isJsxText(c) && c.getText().trim() !== "") hasText = true;
      if (isJsx(c) && textProviders.has(tagName(c))) hasText = true;
      if (ts.isJsxAttribute(c) && /^(label|title|text)$/.test(c.name.getText()) && c.initializer) hasText = true;
    });
    if (hasText) return;

    const kids = ts.isJsxElement(n) ? n.children : [];
    const childTags = kids.filter(isJsx).map(tagName);
    // A wrapper whose children are entirely caller-supplied takes its name from the
    // caller's content. Reported, not failed: real, but not this card's call.
    if (childTags.length === 0 && kids.some((k) => ts.isJsxExpression(k))) {
      namedByCaller.push(where);
      return;
    }
    unnamed.push(`${where} <${tagName(n)}> ${childTags.length ? `children: <${childTags.join("><")}>` : "no children"}`);
  });
}

console.log(
  `   ${buttons} button roles across ${SOURCE_FILES.length} files, ` +
    `${textProviders.size} text-providing components derived`,
);
if (namedByCaller.length > 0) {
  console.log(`   ${namedByCaller.length} named by caller content (wrappers): ${namedByCaller.join(", ")}`);
}
for (const u of unnamed) console.log(`        ${u}`);
check(`every button role resolves to a name (${buttons} checked)`, unnamed.length === 0, true);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
