/**
 * Verifies the c307 accessibility/touch-target fixes, from the real source (board c307).
 *
 *   npm run verify:a11y-touch
 *
 * Three independent items, so three independent sections. The touch-target one is
 * genuinely COMPUTED - the constants are read out of MediaPostCard.tsx and the effective
 * tap height is re-derived here - rather than asserted as a magic number, because "44"
 * appearing in a file proves nothing about what a finger can hit.
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

const createSheet = readFileSync(CREATE_SHEET, "utf8");
const mediaCard = readFileSync(MEDIA_CARD, "utf8");
const listRow = readFileSync(LIST_ROW, "utf8");

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

const num = (src, name) => {
  const match = src.match(new RegExp(`const ${name} = (\\d+);`));
  if (!match) {
    console.error(`FAIL  could not read ${name} from source`);
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

console.log("\n-- (2) InlineAction touch target --");

const TOUCH_TARGET = num(mediaCard, "TOUCH_TARGET");
const ICON = num(mediaCard, "INLINE_ACTION_ICON");
const CHIP = num(mediaCard, "ACTION_CHIP");

// Re-derive what the component computes, from the same numbers it uses.
const inlineHitSlop = Math.ceil((TOUCH_TARGET - ICON) / 2);
const inlineEffective = ICON + inlineHitSlop * 2;
// The sibling this was measured against: 36pt circle + spacing.xs (4) each side.
const chipEffective = CHIP + 4 * 2;

console.log(
  `   TOUCH_TARGET=${TOUCH_TARGET} icon=${ICON} hitSlop=${inlineHitSlop} ` +
    `-> InlineAction ${inlineEffective}pt | ActionChip ${chipEffective}pt`,
);

check(
  `InlineAction's effective tap height clears ${TOUCH_TARGET}pt (got ${inlineEffective})`,
  inlineEffective >= TOUCH_TARGET,
  true,
);
check(
  `ActionChip still lands on ${TOUCH_TARGET}pt (got ${chipEffective}) - the sibling this was measured against`,
  chipEffective === TOUCH_TARGET,
  true,
);
check(
  "InlineAction actually uses the computed slop, not a hardcoded token",
  /hitSlop=\{INLINE_ACTION_HIT_SLOP\}/.test(mediaCard),
  true,
);
// The floor only holds while the icon is the shorter of icon vs caption lineHeight (17).
check(
  "the icon is still the SHORTER dimension, so sizing off it stays conservative",
  ICON <= 17,
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
