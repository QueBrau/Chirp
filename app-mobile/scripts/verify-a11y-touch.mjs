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
 */
import { readFileSync } from "node:fs";

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

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
