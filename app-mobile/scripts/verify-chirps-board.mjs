/**
 * Verifies the Chirps board's two launch-blocking invariants (board cards c297, c298):
 * the pinned-palette rule that keeps the vote score visible in dark mode, and the load
 * states that keep a failed fetch from rendering as an empty board.
 *
 *   npm run verify:chirps-board
 *
 * WHY THIS EXISTS AS A SCRIPT RATHER THAN A SCREENSHOT. The bug was a color resolving
 * through the LIVE theme onto a card that is pinned LIGHT in both schemes, and a
 * screenshot proves it for one build on one day. This computes the actual WCAG contrast
 * from the real palette source, so the regression cannot come back quietly — and it
 * asserts the BROKEN pairing is still broken, which is the falsification kept
 * permanently rather than performed once.
 *
 * Same approach as the sibling verify-*.mjs scripts: values are extracted from the real
 * source at run time, never hand-copied, so this cannot drift into agreeing with itself.
 */
import { readFileSync } from "node:fs";

const COLORS = new URL("../src/theme/colors.ts", import.meta.url);
const VOTE_PILL = new URL("../src/components/VotePill.tsx", import.meta.url);
const CHIRPS_SCREEN = new URL("../app/(tabs)/chirps/index.tsx", import.meta.url);

const colorsSrc = readFileSync(COLORS, "utf8");

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

/** The body of `export const <name>: Palette = { ... };` from the real source. */
function paletteBlock(name) {
  const start = colorsSrc.indexOf(`export const ${name}: Palette = {`);
  if (start === -1) {
    console.error(`FAIL  could not find palette "${name}" in colors.ts`);
    process.exit(1);
  }
  const end = colorsSrc.indexOf("\n};", start);
  return colorsSrc.slice(start, end);
}

function inkOf(name) {
  const match = paletteBlock(name).match(/\n\s*ink:\s*"(#[0-9A-Fa-f]{6})"/);
  if (!match) {
    console.error(`FAIL  could not read ${name}.ink`);
    process.exit(1);
  }
  return match[1];
}

function chirpTintsOf(name) {
  const block = paletteBlock(name);
  const start = block.indexOf("chirpTints:");
  const slice = block.slice(start, block.indexOf("]", start));
  const tints = slice.match(/#[0-9A-Fa-f]{6}/g) ?? [];
  if (tints.length !== 4) {
    console.error(`FAIL  expected 4 chirpTints in ${name}, found ${tints.length}`);
    process.exit(1);
  }
  return tints;
}

/** WCAG 2.1 relative luminance + contrast ratio. */
function luminance(hex) {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r, g, b] = channels.map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const lightInk = inkOf("light");
const darkInk = inkOf("dark");
const tints = chirpTintsOf("light");

console.log(`light.ink ${lightInk} | dark.ink ${darkInk} | tints ${tints.join(" ")}\n`);

// --- the fixed state: pinned light ink is readable on every card tint ---
for (const tint of tints) {
  const ratio = contrast(lightInk, tint);
  check(
    `light.ink on ${tint} clears WCAG AA body text (4.5:1) — got ${ratio.toFixed(1)}:1`,
    ratio >= 4.5,
    true,
  );
}

// --- the falsification, kept rather than performed once ---
//
// This is the pairing the bug produced: the live palette in system dark mode drew the
// score in dark.ink on a light chirp tint. If this ever starts PASSING, either the
// palettes moved far enough that the whole pinning rule needs rethinking, or someone
// "simplified" the tints — either way the c297 reasoning no longer applies and a human
// has to look. It is not asserting that a bug exists; it is asserting that the thing we
// went to the trouble of preventing is still worth preventing.
for (const tint of tints) {
  const ratio = contrast(darkInk, tint);
  check(
    `dark.ink on ${tint} is still unreadable (<3:1) — the pairing c297 prevents, ${ratio.toFixed(1)}:1`,
    ratio < 3,
    true,
  );
}

// --- the wiring: contrast is only safe while the pin is actually passed ---
const votePillSrc = readFileSync(VOTE_PILL, "utf8");
const chirpsSrc = readFileSync(CHIRPS_SCREEN, "utf8");

check(
  "VotePill accepts a pinned palette and falls back to the live one",
  /palette\s*=\s*pinnedPalette\s*\?\?\s*livePalette/.test(votePillSrc),
  true,
);
check(
  "VotePill resolves the score color itself, not through AppText's live-theme tone",
  /const resolvedScoreColor\s*=/.test(votePillSrc) &&
    /style=\{\{ color: resolvedScoreColor \}\}/.test(votePillSrc),
  true,
);
check(
  "chirps/index.tsx actually passes palette={light} — without this the contrast above is theoretical",
  /<VotePill[\s\S]{0,600}?palette=\{light\}/.test(chirpsSrc),
  true,
);

// --- a11y: the core voting control must not be an unlabeled button ---
for (const label of ["Upvote", "Downvote"]) {
  check(
    `VoteGlyph is given accessibilityLabel="${label}"`,
    votePillSrc.includes(`accessibilityLabel="${label}"`),
    true,
  );
}
check(
  "VoteGlyph passes the label through to the Pressable (a prop nothing reads is not a label)",
  /accessibilityRole="button"\s*\n\s*accessibilityLabel=\{accessibilityLabel\}/.test(votePillSrc),
  true,
);

// ---------------------------------------------------------------------------
// c298: a failed load must never be indistinguishable from a quiet campus.
//
// The render is JSX and cannot be executed here the way verify-ws-url runs wsUrl(), so
// these are source assertions on the SHAPE that was wrong: a catch that touched no
// state, and a render whose only branch was "empty or list". They catch the regressions
// that actually happen - someone deleting a branch, or reordering so `loading` is
// treated as loaded. What they cannot do is prove the rendered pixels; that needs the
// device pass, and the PR says so rather than implying otherwise.
// ---------------------------------------------------------------------------

check(
  "chirps screen tracks a real LoadState",
  /type LoadState = "loading" \| "loaded" \| "error";/.test(chirpsSrc),
  true,
);
check(
  "the catch sets an error state instead of leaving chirps null forever (c298's bug)",
  /\} catch \{[\s\S]{0,400}?setLoadState\("error"\);/.test(chirpsSrc),
  true,
);
check(
  "a failed load renders its own state with a retry, not the empty-board copy",
  /loadState === "error" \?[\s\S]{0,400}?actionLabel="Try again"/.test(chirpsSrc),
  true,
);
check(
  "an in-flight first load renders a loading state rather than blank space",
  /loadState === "loading" \?[\s\S]{0,200}?Loading the board/.test(chirpsSrc),
  true,
);

// Order matters as much as presence: if the empty-board branch is evaluated before the
// error/loading ones, a failed load renders "Quiet campus" again and the fix is undone
// while every check above still passes.
const errorAt = chirpsSrc.indexOf('loadState === "error"');
const loadingAt = chirpsSrc.indexOf('loadState === "loading"');
const emptyAt = chirpsSrc.indexOf("Quiet campus");
check(
  "error and loading are branched BEFORE the quiet-campus copy",
  errorAt !== -1 && loadingAt !== -1 && emptyAt !== -1 && errorAt < loadingAt && loadingAt < emptyAt,
  true,
);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
