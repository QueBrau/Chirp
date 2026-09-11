/**
 * Verifies the `secondary` Button variant's label stays legible against its own
 * translucent fill in BOTH modes (board c386 dark mode, board c397 light mode).
 *
 *   npm run verify:button-contrast
 *
 * THE DEFECT (c386, dark). `secondary` fills with `accentSoft` (accent
 * alpha-composited at 16% dark / 15% light over `bg`, appearance.tsx/orgScope.tsx)
 * and labels with the raw accent. That is fine when the accent itself is light
 * enough to clear AA against its own faint wash, and silently wrong when it is
 * not: UNCG's navy, the default campus primary, measures ~1.2:1 against its own
 * dark-mode fill -- the label is effectively invisible.
 *
 * THE DEFECT (c397, light). The same shape recurs in light mode: the default
 * violet accent measures ~3.79:1 and Alpha Delta Pi's azure ~2.49:1 against their
 * own light fill, both below the 4.5:1 AA threshold. c386 left light mode an
 * unconditional identity return; c397 extends the same fix to it.
 *
 * `secondaryLabelColor()` (theme/colorUtils.ts) fixes both: it lifts a too-dark
 * accent in dark mode, or darkens a too-light accent in light mode, in the
 * smallest (1/64) steps that clear 4.5:1, returning the accent unchanged when it
 * already clears that threshold.
 *
 * Same approach as the sibling verify-*.mjs scripts: every color measured here is
 * extracted from the real source at run time (colors.ts, mocks/data.ts,
 * Button.tsx) rather than hand-copied, so this cannot drift into agreeing with
 * itself. The pure math (compositeOver/secondaryLabelColor/contrastRatio/darken)
 * is compiled and imported via the same chart-verify pipeline verify-charts.mjs
 * uses for geometry.ts/treasury.ts (see tsconfig.chart-verify.json).
 *
 * The DARK MODE byte-pin table below (five literal hex pins) was derived by
 * materializing origin/main's colorUtils.ts (the version before this card's edit)
 * into a standalone directory outside the repo, compiling it with the project's
 * own tsc, and running secondaryLabelColor() against it directly -- independent
 * of the edited function under test in this repo, so the pins cannot silently
 * agree with a broken dark branch. See the PR body for the exact values and the
 * confirmation that they match this file's own dark-mode output.
 */
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";

const COLORS_URL = new URL("../src/theme/colors.ts", import.meta.url);
const DATA_URL = new URL("../src/mocks/data.ts", import.meta.url);
const BUTTON_URL = new URL("../src/components/Button.tsx", import.meta.url);

const colorsSrc = readFileSync(COLORS_URL, "utf8");
const dataSrc = readFileSync(DATA_URL, "utf8");
const buttonSrc = readFileSync(BUTTON_URL, "utf8");

// Same chart-verify pattern as verify-charts.mjs: the compiled output is ESM while
// app-mobile's own package.json is not a module, so a type marker beside the build
// scopes the module system to .chart-verify/ instead. Written unconditionally (not
// only by verify:charts) so this guard also works when run on its own.
const OUT = new URL("../.chart-verify/", import.meta.url);
mkdirSync(OUT, { recursive: true });
writeFileSync(new URL("./package.json", OUT), '{"type":"module"}\n');

const { compositeOver, secondaryLabelColor, contrastRatio, darken, SECONDARY_FILL_ALPHA_DARK, SECONDARY_FILL_ALPHA_LIGHT } =
  await import("../.chart-verify/theme/colorUtils.js");

/**
 * HSL hue in degrees (0-360), or `null` for an achromatic color (r === g === b).
 * Used only to sanity-check that a LIFTED label still reads as "the same color
 * family" as the accent it was lifted from -- catches a regression that collapses
 * to an unrelated fallback (e.g. the generic `ink`) while still passing a bare
 * contrast-ratio check, since `ink` is near-white/desaturated and its hue sits
 * measurably away from any of the roster's saturated accents (verified below).
 */
function hue(hex) {
  const int = parseInt(hex.replace("#", ""), 16);
  let r = ((int >> 16) & 255) / 255;
  let g = ((int >> 8) & 255) / 255;
  let b = (int & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  if (d === 0) return null;
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  if (h < 0) h += 360;
  return h;
}

/** Circular hue distance in degrees (0-180). `null` on either side means "far". */
function hueDistance(hexA, hexB) {
  const ha = hue(hexA);
  const hb = hue(hexB);
  if (ha === null || hb === null) return 180;
  const d = Math.abs(ha - hb) % 360;
  return d > 180 ? 360 - d : d;
}

const HUE_TOLERANCE_DEG = 5;

let failures = 0;
const check = (name, cond, extra = "") => {
  if (cond) {
    console.log(`  PASS  ${name}`);
  } else {
    console.log(`  FAIL  ${name} ${extra}`);
    failures++;
  }
};

// ---------------------------------------------------------------------------
// Read every color from the real source, never hand-copied.
// ---------------------------------------------------------------------------

/** The body of `export const <name>: Palette = { ... };` from colors.ts. */
function paletteBlock(name) {
  const start = colorsSrc.indexOf(`export const ${name}: Palette = {`);
  if (start === -1) {
    console.error(`FAIL  could not find palette "${name}" in colors.ts`);
    process.exit(1);
  }
  const end = colorsSrc.indexOf("\n};", start);
  return colorsSrc.slice(start, end);
}

function fieldOf(block, field, label) {
  const match = block.match(new RegExp(`\\n\\s*${field}:\\s*"(#[0-9A-Fa-f]{6})"`));
  if (!match) {
    console.error(`FAIL  could not read ${label ?? field}`);
    process.exit(1);
  }
  return match[1];
}

const darkBlock = paletteBlock("dark");
const lightBlock = paletteBlock("light");

const DARK_BG = fieldOf(darkBlock, "bg", "dark.bg");
const DARK_INK = fieldOf(darkBlock, "ink", "dark.ink");
const DARK_ACCENT = fieldOf(darkBlock, "accent", "dark.accent");
const LIGHT_BG = fieldOf(lightBlock, "bg", "light.bg");
const LIGHT_INK = fieldOf(lightBlock, "ink", "light.ink");
const LIGHT_ACCENT = fieldOf(lightBlock, "accent", "light.accent");

const brandMatch = colorsSrc.match(/export const brand = "(#[0-9A-Fa-f]{6})";/);
if (!brandMatch) {
  console.error("FAIL  could not read `brand` from colors.ts");
  process.exit(1);
}
const BRAND = brandMatch[1];

/** `export const <name>: <Type> = { ... };` body from mocks/data.ts. */
function dataBlock(name) {
  const start = dataSrc.indexOf(`export const ${name}`);
  if (start === -1) {
    console.error(`FAIL  could not find "${name}" in mocks/data.ts`);
    process.exit(1);
  }
  const end = dataSrc.indexOf("\n};", start);
  return dataSrc.slice(start, end);
}

function primaryOf(block, label) {
  const match = block.match(/primary:\s*"(#[0-9A-Fa-f]{6})"/);
  if (!match) {
    console.error(`FAIL  could not read ${label}.colors.primary`);
    process.exit(1);
  }
  return match[1];
}

const UNCG_NAVY = primaryOf(dataBlock("MOCK_CAMPUS_COLORS"), "MOCK_CAMPUS_COLORS");
const SIGMA_CHI = primaryOf(dataBlock("MOCK_CHAPTER"), "MOCK_CHAPTER");
const ALPHA_DELTA_PI = primaryOf(dataBlock("MOCK_CHAPTER_ADPI"), "MOCK_CHAPTER_ADPI");

// DEFAULT_ORG_COLORS.primary is the identifier `brand`, not a literal hex -- confirm
// that is still true (so this cannot silently start reading a stale value if the
// source ever switches to a literal), then resolve it to the real brand hex above.
const defaultOrgMatch = dataSrc.match(
  /export const DEFAULT_ORG_COLORS: CampusColors = \{\s*primary:\s*(\w+),/,
);
if (!defaultOrgMatch) {
  console.error("FAIL  could not read DEFAULT_ORG_COLORS.primary from mocks/data.ts");
  process.exit(1);
}
check("DEFAULT_ORG_COLORS.primary is the `brand` identifier", defaultOrgMatch[1] === "brand", defaultOrgMatch[1]);
const DEFAULT_ORG_PRIMARY = BRAND;

console.log(`  roster: UNCG navy ${UNCG_NAVY}, default dark accent ${DARK_ACCENT}, Sigma Chi ${SIGMA_CHI}, Alpha Delta Pi ${ALPHA_DELTA_PI}, DEFAULT_ORG_COLORS fallback ${DEFAULT_ORG_PRIMARY}`);

// ---------------------------------------------------------------------------
// Dark mode: every roster color's label must clear 4.5:1 against its own fill.
// ---------------------------------------------------------------------------

console.log("\nDARK MODE — label clears AA against its own composited fill");
for (const [label, accent] of [
  ["UNCG navy", UNCG_NAVY],
  ["default dark accent", DARK_ACCENT],
  ["Sigma Chi", SIGMA_CHI],
  ["Alpha Delta Pi", ALPHA_DELTA_PI],
  ["DEFAULT_ORG_COLORS fallback", DEFAULT_ORG_PRIMARY],
]) {
  const fill = compositeOver(accent, SECONDARY_FILL_ALPHA_DARK, DARK_BG);
  const result = secondaryLabelColor(accent, DARK_BG, "dark", DARK_INK);
  const ratio = contrastRatio(result, fill);
  check(`${label}: secondaryLabelColor clears 4.5:1 against its fill`, ratio >= 4.5, `ratio=${ratio.toFixed(3)}`);

  // This color's own raw contrast against its fill decides whether a LIFT was
  // actually required. For every color that needed a lift, a bare ">= 4.5" check
  // alone cannot tell "correctly lightened toward the accent" apart from "lift
  // loop failed to converge and fell through to the generic `ink` fallback" --
  // both pass a contrast-only check since `ink` is near-white and clears 4.5:1
  // against almost any dark fill. Guard against that collapse two ways: the
  // result must not literally BE `ink`, and it must stay hue-close to the
  // original accent (a collapse to `ink`, or to some other unrelated fallback,
  // reads as a different color family, not a lightened version of this one).
  const rawRatio = contrastRatio(accent, fill);
  if (rawRatio < 4.5) {
    check(`${label}: lifted result is not the generic ink fallback`, result !== DARK_INK, result);
    const dist = hueDistance(accent, result);
    check(
      `${label}: lifted result stays hue-close to the original accent (<=${HUE_TOLERANCE_DEG} deg)`,
      dist <= HUE_TOLERANCE_DEG,
      `hue distance=${dist.toFixed(1)} deg, accent=${accent}, result=${result}`,
    );
  }
}

// Regression trap (pins the defect, mirrors verify-chirps-board.mjs): the RAW,
// unlifted UNCG navy against its own fill must stay broken. If this ever passes,
// either the fill formula or dark.bg changed enough to accidentally fix the
// defect by coincidence, which is itself worth knowing.
{
  const fill = compositeOver(UNCG_NAVY, SECONDARY_FILL_ALPHA_DARK, DARK_BG);
  const rawRatio = contrastRatio(UNCG_NAVY, fill);
  check(
    "regression trap: raw UNCG navy on its own dark fill stays broken (~1.2:1)",
    rawRatio < 4.5 && rawRatio > 1.0 && rawRatio < 1.5,
    `ratio=${rawRatio.toFixed(3)}`,
  );
}

// An accent that already clears 4.5:1 unlifted must come back UNCHANGED -- an
// always-lift implementation would also pass the check above while silently
// relabeling every screen's existing violet secondary buttons.
{
  const fill = compositeOver(DARK_ACCENT, SECONDARY_FILL_ALPHA_DARK, DARK_BG);
  const rawRatio = contrastRatio(DARK_ACCENT, fill);
  check("default dark accent already clears AA unlifted (precondition)", rawRatio >= 4.5, `ratio=${rawRatio.toFixed(3)}`);
  const result = secondaryLabelColor(DARK_ACCENT, DARK_BG, "dark", DARK_INK);
  check("default dark accent: secondaryLabelColor returns it UNCHANGED (identity)", result === DARK_ACCENT, result);
}

// ---------------------------------------------------------------------------
// Dark mode byte-pin table (c397): every roster color's dark-mode output must
// stay EXACTLY the literal it produced before this card's light-mode edit --
// not just ">= 4.5" (the loop above already covers that), but the precise hex,
// so a mistake that shares MIN_CONTRAST/STEP/SECONDARY_FILL_ALPHA_DARK between
// the two branches, or that accidentally routes a dark call through the new
// light branch, is caught even if it happens to still clear AA. Pins were
// derived from origin/main's colorUtils.ts (the pre-c397 algorithm), compiled
// and run standalone outside this repo -- see the file header comment and the
// PR body for the exact procedure.
// ---------------------------------------------------------------------------

console.log("\nDARK MODE — byte-identical to the pre-c397 pinned output");
for (const [label, accent, pinned] of [
  ["UNCG navy", UNCG_NAVY, "#728091"],
  ["default dark accent", DARK_ACCENT, "#7C7CFF"],
  ["Sigma Chi", SIGMA_CHI, "#6981b8"],
  ["Alpha Delta Pi", ALPHA_DELTA_PI, "#2E9BD6"],
  ["DEFAULT_ORG_COLORS fallback", DEFAULT_ORG_PRIMARY, "#7477f3"],
]) {
  const result = secondaryLabelColor(accent, DARK_BG, "dark", DARK_INK);
  check(`${label}: dark-mode output matches the pinned pre-c397 value (${pinned})`, result === pinned, `got=${result}`);
}

// ---------------------------------------------------------------------------
// Light mode (c397): mirrors the dark-mode fix. Every roster color's label
// must clear 4.5:1 against its own composited light fill; an already-legible
// accent must come back UNCHANGED; a darkened accent must stay hue-close, must
// not collapse to the generic ink fallback, and must be the FIRST step that
// clears (not an over-darkened later one). Two of these colors independently
// measure below 4.5:1 in LIGHT mode today (default light accent ~3.79:1, Alpha
// Delta Pi ~2.49:1) -- that gap was c386's known, out-of-scope defect and is
// exactly what this card closes.
// ---------------------------------------------------------------------------

const LIGHT_ROSTER = [
  ["UNCG navy", UNCG_NAVY],
  ["Sigma Chi", SIGMA_CHI],
  ["Alpha Delta Pi", ALPHA_DELTA_PI],
  ["default light accent", LIGHT_ACCENT],
  ["DEFAULT_ORG_COLORS fallback", DEFAULT_ORG_PRIMARY],
];

console.log("\nLIGHT MODE — label clears AA against its own composited fill");
const lightTableRows = [];
for (const [label, accent] of LIGHT_ROSTER) {
  const fill = compositeOver(accent, SECONDARY_FILL_ALPHA_LIGHT, LIGHT_BG);
  const rawRatio = contrastRatio(accent, fill);
  const result = secondaryLabelColor(accent, LIGHT_BG, "light", LIGHT_INK);
  const resultRatio = contrastRatio(result, fill);

  check(`${label}: secondaryLabelColor clears 4.5:1 against its fill`, resultRatio >= 4.5, `ratio=${resultRatio.toFixed(3)}`);

  if (rawRatio >= 4.5) {
    // Already legible unlifted: must come back byte-identical, not merely "also
    // clears 4.5:1" -- an always-darken implementation would pass the check
    // above while silently relabeling every screen's existing navy/blue
    // secondary buttons.
    check(`${label}: already clears AA unlifted, returns it UNCHANGED (identity)`, result === accent, result);
    lightTableRows.push({ label, accent, rawRatio, action: "unchanged", result, resultRatio });
  } else {
    // Needed darkening: guard against a collapse to the generic ink fallback
    // (near-white, would still pass a bare contrast check) and against hue
    // drift, exactly mirroring the dark-mode guards above.
    check(`${label}: darkened result is not the generic ink fallback`, result !== LIGHT_INK, result);
    const dist = hueDistance(accent, result);
    check(
      `${label}: darkened result stays hue-close to the original accent (<=${HUE_TOLERANCE_DEG} deg)`,
      dist <= HUE_TOLERANCE_DEG,
      `hue distance=${dist.toFixed(1)} deg, accent=${accent}, result=${result}`,
    );

    // Independently re-derive the step count by scanning darken() from step 1,
    // using the same imported darken/contrastRatio the function under test
    // uses internally -- not by reading secondaryLabelColor's internals. The
    // FIRST step that clears must equal `result` byte-for-byte, catching a
    // sabotage that darkens against the wrong fill (e.g. the dark alpha) and
    // still happens to clear the real threshold.
    let step = 1;
    let derived = null;
    while (step <= 64) {
      const candidate = darken(accent, step * (1 / 64));
      if (contrastRatio(candidate, fill) >= 4.5) {
        derived = candidate;
        break;
      }
      step += 1;
    }
    check(`${label}: independently re-derived darken() step matches secondaryLabelColor's output`, derived === result, `derived=${derived} step=${step}/64`);

    // Tie the "first clearing step" property to `result` itself rather than to
    // the independent scan above: find which step k actually PRODUCES `result`
    // (darken(accent, k/64) === result), then require the step before k to
    // still fail AA. Using the scan's own `step` here instead would be
    // vacuous -- that loop stops at its own first clearing step by
    // construction, so "the step before it fails" would trivially hold no
    // matter what `result` actually was; anchoring on `result` makes this
    // assertion react to what the function under test actually returned.
    let resultStep = null;
    for (let k = 1; k <= 64; k++) {
      if (darken(accent, k * (1 / 64)) === result) {
        resultStep = k;
        break;
      }
    }
    check(`${label}: result matches some darken() step (not a value the loop cannot reach)`, resultStep !== null, `result=${result}`);
    if (resultStep !== null) {
      const prevCandidate = resultStep > 1 ? darken(accent, (resultStep - 1) * (1 / 64)) : accent;
      const prevRatio = contrastRatio(prevCandidate, fill);
      check(
        `${label}: the step before the returned one still fails AA (first-clearing-step, not over-darkened)`,
        prevRatio < 4.5,
        `resultStep=${resultStep}/64 prevRatio=${prevRatio.toFixed(3)}`,
      );
    }

    lightTableRows.push({ label, accent, rawRatio, action: `darkened (${step}/64)`, result, resultRatio });
  }
}

// Regression trap (pins the defect, mirrors the dark-mode trap above): the
// RAW, unlifted default light accent and Alpha Delta Pi ratios against their
// own light fill must stay inside the exact measured band. This does not
// change before/after the fix (it measures the raw accent, not the corrected
// output) -- it is a silent-drift trap: if the roster or the light fill alpha
// ever changes without updating this pin, it goes red.
{
  const fill = compositeOver(LIGHT_ACCENT, SECONDARY_FILL_ALPHA_LIGHT, LIGHT_BG);
  const rawRatio = contrastRatio(LIGHT_ACCENT, fill);
  check(
    "regression trap: raw default light accent on its own light fill stays inside the measured band (~3.79:1)",
    rawRatio > 3.7 && rawRatio < 3.9 && rawRatio < 4.5,
    `ratio=${rawRatio.toFixed(3)}`,
  );
}
{
  const fill = compositeOver(ALPHA_DELTA_PI, SECONDARY_FILL_ALPHA_LIGHT, LIGHT_BG);
  const rawRatio = contrastRatio(ALPHA_DELTA_PI, fill);
  check(
    "regression trap: raw Alpha Delta Pi on its own light fill stays inside the measured band (~2.49:1)",
    rawRatio > 2.4 && rawRatio < 2.6 && rawRatio < 4.5,
    `ratio=${rawRatio.toFixed(3)}`,
  );
}

// Before/after table for the PR body (DESIGN NOTE: braul/Jose need the exact
// hex and ratio per accent to sign off on this visible light-mode color
// change).
console.log("\nLIGHT MODE — before/after table");
console.log("  label | accent | raw ratio | action | result | result ratio");
for (const row of lightTableRows) {
  console.log(
    `  ${row.label} | ${row.accent} | ${row.rawRatio.toFixed(3)} | ${row.action} | ${row.result} | ${row.resultRatio.toFixed(3)}`,
  );
}

// ---------------------------------------------------------------------------
// Source: Button.tsx's `secondary` case actually calls the helper (syntax-
// anchored on the switch-case structure, not on comment prose).
// ---------------------------------------------------------------------------

console.log("\nSOURCE — Button.tsx's secondary case calls secondaryLabelColor()");
{
  const caseStart = buttonSrc.indexOf('case "secondary":');
  if (caseStart === -1) {
    console.error('FAIL  could not find case "secondary": in Button.tsx');
    process.exit(1);
  }
  const nextCase = buttonSrc.indexOf("case ", caseStart + 1);
  const switchEnd = buttonSrc.indexOf("\n  }", caseStart);
  const sliceEnd = nextCase === -1 ? switchEnd : Math.min(nextCase, switchEnd === -1 ? Infinity : switchEnd);
  const secondaryCase = buttonSrc.slice(caseStart, sliceEnd);
  check("secondary case calls secondaryLabelColor(", /secondaryLabelColor\s*\(/.test(secondaryCase));
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
