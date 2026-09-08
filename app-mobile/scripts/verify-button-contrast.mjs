/**
 * Verifies the `secondary` Button variant's label stays legible against its own
 * translucent fill in dark mode (board c386).
 *
 *   npm run verify:button-contrast
 *
 * THE DEFECT. `secondary` fills with `accentSoft` (accent alpha-composited at 16%
 * dark / 15% light over `bg`, appearance.tsx/orgScope.tsx) and labels with the raw
 * accent. That is fine when the accent itself is light enough to clear AA against
 * its own faint wash, and silently wrong when it is not: UNCG's navy, the default
 * campus primary, measures ~1.2:1 against its own dark-mode fill -- the label is
 * effectively invisible. `secondaryLabelColor()` (theme/colorUtils.ts) fixes this
 * by lifting a too-dark accent in dark mode only, in the smallest steps that clear
 * 4.5:1, and leaves light mode untouched by construction.
 *
 * Same approach as the sibling verify-*.mjs scripts: every color measured here is
 * extracted from the real source at run time (colors.ts, mocks/data.ts,
 * Button.tsx) rather than hand-copied, so this cannot drift into agreeing with
 * itself. The pure math (compositeOver/secondaryLabelColor/contrastRatio) is
 * compiled and imported via the same chart-verify pipeline verify-charts.mjs uses
 * for geometry.ts/treasury.ts (see tsconfig.chart-verify.json).
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

const { compositeOver, secondaryLabelColor, contrastRatio, SECONDARY_FILL_ALPHA } = await import(
  "../.chart-verify/theme/colorUtils.js"
);

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
  const fill = compositeOver(accent, SECONDARY_FILL_ALPHA, DARK_BG);
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
  const fill = compositeOver(UNCG_NAVY, SECONDARY_FILL_ALPHA, DARK_BG);
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
  const fill = compositeOver(DARK_ACCENT, SECONDARY_FILL_ALPHA, DARK_BG);
  const rawRatio = contrastRatio(DARK_ACCENT, fill);
  check("default dark accent already clears AA unlifted (precondition)", rawRatio >= 4.5, `ratio=${rawRatio.toFixed(3)}`);
  const result = secondaryLabelColor(DARK_ACCENT, DARK_BG, "dark", DARK_INK);
  check("default dark accent: secondaryLabelColor returns it UNCHANGED (identity)", result === DARK_ACCENT, result);
}

// ---------------------------------------------------------------------------
// Light mode: byte-identical to today, by IDENTITY, never by a contrast
// threshold -- two of these colors independently measure below 4.5:1 in LIGHT
// mode today (default light accent ~3.79:1, Alpha Delta Pi ~2.49:1) and that
// pre-existing gap is explicitly out of scope for c386.
// ---------------------------------------------------------------------------

console.log("\nLIGHT MODE — output is byte-identical to the input accent (identity, not a threshold)");
for (const [label, accent] of [
  ["UNCG navy", UNCG_NAVY],
  ["Sigma Chi", SIGMA_CHI],
  ["Alpha Delta Pi", ALPHA_DELTA_PI],
  ["default light accent", LIGHT_ACCENT],
  ["DEFAULT_ORG_COLORS fallback", DEFAULT_ORG_PRIMARY],
]) {
  const result = secondaryLabelColor(accent, LIGHT_BG, "light", LIGHT_INK);
  check(`${label}: light mode returns the accent unchanged`, result === accent, result);
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
