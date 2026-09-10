/**
 * Verifies the Chirps board's two launch-blocking invariants (board cards c297, c298):
 * that the vote score is readable on a chirp card in BOTH schemes, and the load states
 * that keep a failed fetch from rendering as an empty board.
 *
 *   npm run verify:chirps-board
 *
 * WHAT c384 CHANGED, and why this script survived it rather than being deleted. The
 * c297 bug was a colour resolving through the LIVE theme onto a card PINNED to the
 * light palette: in system dark mode the score drew as dark.ink (near-white) on a
 * near-white pastel tint and vanished. The fix was to pin the score too, and the
 * checks below asserted that pin.
 *
 * c384 retired the tints and the pin together - chirp cards are now `surface` and
 * follow the system scheme like every other card. That makes the c297 pairing
 * STRUCTURALLY IMPOSSIBLE rather than prevented: there is no longer a second palette
 * on this screen for the live one to disagree with. So the assertions are rewritten
 * to the new invariant instead of dropped, because "the bug cannot happen any more"
 * is a claim that needs a guard exactly as much as the pin did - the way it comes
 * back is someone reintroducing a pinned palette here.
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

function surfaceOf(name) {
  const match = paletteBlock(name).match(/\n\s*surface:\s*"(#[0-9A-Fa-f]{6})"/);
  if (!match) {
    console.error(`FAIL  could not read ${name}.surface`);
    process.exit(1);
  }
  return match[1];
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
const lightSurface = surfaceOf("light");
const darkSurface = surfaceOf("dark");

console.log(
  `light ${lightInk} on ${lightSurface} | dark ${darkInk} on ${darkSurface}\n`,
);

// --- the invariant: each mode's ink is readable on that mode's card ---
//
// A chirp card is `surface` in both modes now, and its content resolves through the
// live palette, so the only pairing that can ever render is same-mode ink on same-mode
// surface. Both must clear AA body text - the score is the number c297 was about.
for (const [mode, ink, surface] of [
  ["light", lightInk, lightSurface],
  ["dark", darkInk, darkSurface],
]) {
  const ratio = contrast(ink, surface);
  check(
    `${mode}.ink on ${mode}.surface clears WCAG AA body text (4.5:1) — got ${ratio.toFixed(1)}:1`,
    ratio >= 4.5,
    true,
  );
}

// --- the falsification, kept rather than performed once ---
//
// The CROSS-mode pairings are the ones that must never render, and they are exactly
// what a reintroduced pin would produce. Asserting they are still unreadable is what
// keeps the c297 reasoning honest: if either ever starts passing, the palettes have
// moved far enough that "the modes cannot be mixed" stopped being load-bearing and a
// human has to re-read this file rather than trust it.
for (const [name, ink, surface] of [
  ["dark.ink on light.surface", darkInk, lightSurface],
  ["light.ink on dark.surface", lightInk, darkSurface],
]) {
  const ratio = contrast(ink, surface);
  check(
    `${name} is still unreadable (<3:1) — the mixing c297 prevents, ${ratio.toFixed(1)}:1`,
    ratio < 3,
    true,
  );
}

// --- the wiring: the invariant above only holds while NOTHING here is pinned ---
const votePillSrc = readFileSync(VOTE_PILL, "utf8");
const chirpsSrc = readFileSync(CHIRPS_SCREEN, "utf8");

// c384. The cross-mode pairings above are unreachable only because this screen has a
// single palette. These two checks are the ones that would catch a pin coming back -
// as a VotePill prop, or as a bare `light.` read anywhere in the screen. Without them
// the contrast section is a statement about colours rather than about the app.
check(
  "VotePill has no palette-pinning prop — it resolves through useTheme() alone",
  !/palette\?:\s*Palette/.test(votePillSrc) && !/pinnedPalette/.test(votePillSrc),
  true,
);
check(
  "chirps/index.tsx pins nothing to the light palette (no `light.` reads, no palette={light})",
  !/\blight\.[a-zA-Z]/.test(chirpsSrc) && !/palette=\{light\}/.test(chirpsSrc),
  true,
);
check(
  "VotePill resolves the score color itself, not through AppText's live-theme tone",
  /const resolvedScoreColor\s*=/.test(votePillSrc) &&
    /style=\{\{ color: resolvedScoreColor \}\}/.test(votePillSrc),
  true,
);
check(
  "chirp cards take their shadow from cardShadow(palette), not a hardcoded light-mode elevation",
  /\.\.\.cardShadow\(palette\)/.test(chirpsSrc) && !/\.\.\.elevation\.card/.test(chirpsSrc),
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
