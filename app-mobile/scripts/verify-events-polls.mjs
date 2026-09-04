/**
 * Verifies the c312 event/RSVP + polls sweep fixes, from the real source.
 *
 *   npm run verify:events-polls
 *
 * The headline finding is not "a missing loading state" — it is that a FAILED FETCH
 * rendered as "This event may have been removed, or it may not be shared with you". A
 * dropped connection told a legitimately invited guest they had been excluded. The
 * c299 class at its worst: affirmatively wrong about the one thing the reader cares
 * about, rather than merely unhelpful. So the checks below care about the DISTINCTION
 * being preserved, not just about a state existing.
 *
 * These are source assertions. They catch the regression — someone collapsing the
 * branches back, or dropping a label — but a KeyboardAvoidingView in source is not a
 * keyboard observed to move and an accessibilityLabel is not VoiceOver heard saying it.
 * Those stay on the device checklist.
 */
import { readFileSync } from "node:fs";

const EVENT = new URL("../app/(tabs)/chapter/event/[id].tsx", import.meta.url);
const SHEET = new URL("../src/components/CreateEventSheet.tsx", import.meta.url);
const POLL = new URL("../src/components/PollCard.tsx", import.meta.url);

const event = readFileSync(EVENT, "utf8");
const sheet = readFileSync(SHEET, "utf8");
const poll = readFileSync(POLL, "utf8");

let failures = 0;
const check = (name, actual, expected = true) => {
  if (actual === expected) {
    console.log(`  PASS  ${name}`);
  } else {
    console.log(`  FAIL  ${name}`);
    console.log(`        expected ${JSON.stringify(expected)}`);
    console.log(`        got      ${JSON.stringify(actual)}`);
    failures++;
  }
};

console.log("\n-- (1) event detail: a failed fetch must not accuse the viewer --");

check("a real LoadState exists", /type LoadState = "loading" \| "loaded" \| "error";/.test(event));
check(
  "the catch tells 404/403 apart from everything else",
  /error instanceof ApiError && \(error\.status === 404 \|\| error\.status === 403\)/.test(event),
);
check(
  "only 404/403 fall through to the removed-or-not-shared copy",
  /setLoadState\(denied \? "loaded" : "error"\)/.test(event),
);
check(
  "a transport failure gets its own state with a retry",
  /loadState === "error"[\s\S]{0,600}?actionLabel="Try again"/.test(event),
);
check(
  "the in-flight state is a visible message, not a bare empty View",
  /loadState === "loading"[\s\S]{0,600}?Loading this event/.test(event),
);
// Order is the whole fix: if the null branch is reached first, an errored load renders
// "may not be shared with you" again and every check above still passes.
const errAt = event.indexOf('loadState === "error"');
const loadAt = event.indexOf('loadState === "loading"');
// Anchored on the JSX prop, not the bare phrase: the phrase also appears in a comment
// ABOVE the branches, which made this check fail on correct code the first time it ran.
const nullAt = event.indexOf('title="Event not found"');
check(
  "loading and error are branched BEFORE the not-found copy",
  loadAt !== -1 && errAt !== -1 && nullAt !== -1 && loadAt < errAt && errAt < nullAt,
);
// The message that made this dangerous must still be reachable for the case it is TRUE
// for - the fix is to stop lying, not to delete the honest copy.
check(
  "the removed-or-not-shared copy still exists for genuine 404/403",
  /may have been removed, or it may not be shared with you/.test(event),
);

console.log("\n-- (2) CreateEventSheet: keyboard + image-only controls --");

check("wraps in KeyboardAvoidingView", /<KeyboardAvoidingView/.test(sheet));
check(
  "behavior is split per platform",
  /behavior=\{Platform\.OS === "ios" \? "padding" : "height"\}/.test(sheet),
);
const kavAt = sheet.indexOf("<KeyboardAvoidingView");
const backdropAt = sheet.indexOf("onPress={close}");
check("the wrapper is OUTSIDE the backdrop (inside, it lifts nothing)", kavAt < backdropAt);
check(
  "a tap on the submit button is not eaten by the keyboard dismiss",
  /keyboardShouldPersistTaps="handled"/.test(sheet),
);
check(
  "cover swatches are named (they contain only an Image, so content supplies no name)",
  /accessibilityLabel=\{`Cover option \$\{index \+ 1\} of \$\{COVER_SEEDS\.length\}`\}/.test(sheet),
);
check(
  "and the decorative Image inside them is not announced separately",
  /accessibilityElementsHidden\s*\n\s*importantForAccessibility="no"/.test(sheet),
);
// The visibility chips are the control group this was measured against: they need no
// label because their own text supplies one. If that ever stops being true, the reason
// the swatches are special stops holding.
check(
  "visibility chips still take their name from text content",
  /\{tier\.label\}/.test(sheet),
);

console.log("\n-- (3) PollCard --");

check(
  'the spoken label says "1 vote", not "1 votes"',
  /option\.votes === 1 \? "1 vote" :/.test(poll),
);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
