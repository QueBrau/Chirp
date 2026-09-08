/**
 * Tiny hex color utility for the campus theming system (DESIGN.md §8.5) — alpha,
 * mix/lighten/darken, and a WCAG relative-luminance contrast check. No new deps;
 * everything here operates on the hex strings already living in Palette tokens.
 */

interface Rgb {
  r: number;
  g: number;
  b: number;
}

/** Parses `#rgb` / `#rrggbb` into 0-255 channels. Bad input falls back to black. */
export function hexToRgb(hex: string): Rgb {
  let normalized = hex.replace("#", "");
  if (normalized.length === 3) {
    normalized = normalized
      .split("")
      .map((channel) => channel + channel)
      .join("");
  }
  const int = parseInt(normalized, 16);
  if (normalized.length !== 6 || Number.isNaN(int)) {
    return { r: 0, g: 0, b: 0 };
  }
  return { r: (int >> 16) & 255, g: (int >> 8) & 255, b: int & 255 };
}

function toHexChannel(value: number): string {
  return Math.round(Math.min(255, Math.max(0, value)))
    .toString(16)
    .padStart(2, "0");
}

function rgbToHex({ r, g, b }: Rgb): string {
  return `#${toHexChannel(r)}${toHexChannel(g)}${toHexChannel(b)}`;
}

/** Returns `hex` at `alpha` (0-1) opacity as an `rgba()` string. */
export function withAlpha(hex: string, alpha: number): string {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${Math.min(1, Math.max(0, alpha))})`;
}

/** Blends `hex` toward `target` by `weight` (0 = `hex`, 1 = `target`). Backs lighten/darken/tint. */
export function mix(hex: string, target: string, weight: number): string {
  const from = hexToRgb(hex);
  const to = hexToRgb(target);
  const w = Math.min(1, Math.max(0, weight));
  return rgbToHex({
    r: from.r + (to.r - from.r) * w,
    g: from.g + (to.g - from.g) * w,
    b: from.b + (to.b - from.b) * w,
  });
}

/** Blends `hex` toward white by `amount` (0-1). Used for gradient-pair endpoints. */
export function lighten(hex: string, amount = 0.18): string {
  return mix(hex, "#FFFFFF", amount);
}

/** Blends `hex` toward black by `amount` (0-1). Used by the accent contrast guard. */
export function darken(hex: string, amount: number): string {
  return mix(hex, "#000000", amount);
}

function linearChannel(channel255: number): number {
  const s = channel255 / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

/** WCAG relative luminance, 0 (black) to 1 (white). */
export function relativeLuminance(hex: string): number {
  const { r, g, b } = hexToRgb(hex);
  return 0.2126 * linearChannel(r) + 0.7152 * linearChannel(g) + 0.0722 * linearChannel(b);
}

/** WCAG contrast ratio of `hex` against opaque white (range 1.0–21.0). */
export function contrastWithWhite(hex: string): number {
  return 1.05 / (relativeLuminance(hex) + 0.05);
}

/**
 * WCAG contrast ratio between any two opaque colors (range 1.0–21.0), c385.
 *
 * contrastWithWhite() above is the special case this generalises, and it is
 * deliberately NOT reimplemented in terms of this one: it is called in a loop by
 * ensureAccentContrast and its single-expression form is exactly right there.
 *
 * BOTH ARGUMENTS MUST BE OPAQUE. relativeLuminance parses `#rrggbb` and has no
 * concept of alpha, so passing an rgba() token (accentSoft, dark chirpTints, the
 * border tokens) silently measures nonsense rather than throwing. Composite first
 * with mix() if you need the contrast of a translucent layer.
 */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}

/**
 * Contrast guard (DESIGN §8.5): campus colors are picked for school pride, not
 * button-text contrast, so a light one (e.g. a gold) can read poorly under white
 * text. Darkens in 8% steps — capped at 8 steps (a ~49% max darken) — until the
 * color clears 3:1 against white, the WCAG AA threshold for bold/large UI text,
 * which is how the accent role is actually used (bodyBold buttons, chip labels,
 * never long-form copy).
 */
export function ensureAccentContrast(hex: string): string {
  const MIN_CONTRAST = 3;
  const STEP = 0.08;
  const MAX_STEPS = 8;

  let color = hex;
  let steps = 0;
  while (contrastWithWhite(color) < MIN_CONTRAST && steps < MAX_STEPS) {
    color = darken(color, STEP);
    steps += 1;
  }
  return color;
}

/**
 * Alpha-composites `fgHex` at `alpha` (0-1) over opaque `bgHex`, returning the
 * resulting opaque hex. This is exactly `mix(bgHex, fgHex, alpha)` -- mix's own
 * definition (`from + (to - from) * weight` per channel) IS the standard "over"
 * compositing formula for an opaque backdrop -- this just names the operation the
 * contrastRatio doc comment above already tells callers to perform by hand before
 * measuring a translucent layer (e.g. `accentSoft`, which is `withAlpha(accent, …)`
 * and therefore not itself opaque).
 */
export function compositeOver(fgHex: string, alpha: number, bgHex: string): string {
  return mix(bgHex, fgHex, alpha);
}

/**
 * The `secondary` fill's alpha (c386). Matches `accentSoft`'s dark-mode alpha in
 * appearance.tsx/orgScope.tsx (0.16) -- duplicated there rather than imported
 * because those two derivation sites predate this card and already had their own
 * literal before secondaryLabelColor existed; not touched here to keep this card's
 * diff to the contrast fix. Exported so at least the verify script (below) and
 * secondaryLabelColor's own fill computation share one source instead of two.
 */
export const SECONDARY_FILL_ALPHA = 0.16;

/**
 * Board c386. `secondary` buttons fill with `accentSoft` (accent alpha-composited
 * at 16% dark / 15% light over `bg`, per appearance.tsx/orgScope.tsx) and label
 * with the raw accent. That reads fine whenever the accent itself is light enough
 * to clear AA against its own faint wash -- it does NOT when the accent is dark
 * (the default campus primary is often a school's navy), because a 15-16% wash of
 * a very dark color barely lifts off a dark canvas: UNCG navy on its own dark
 * fill measures ~1.2:1, effectively invisible.
 *
 * Light mode is untouched BY CONSTRUCTION: there is no branch that can reach it
 * except the unconditional early return, so it is always byte-identical to the
 * raw `accent` -- never gated on a contrast measurement, because two of the real
 * campus/org accents already fall below 4.5:1 in light mode today on their own
 * (a separate, pre-existing, out-of-scope defect).
 *
 * Dark mode composites the real fill the button actually paints (using the
 * caller's live accent/bg, so campus tint and org-scoped accents are honored
 * automatically), and if the raw accent already clears 4.5:1 against that fill it
 * is returned unchanged -- an already-legible accent (e.g. the default violet)
 * must not be silently relabeled. Otherwise the accent is lightened in fine (1/64)
 * steps, returning the FIRST step whose contrast against the fill clears 4.5:1, so
 * the result stays as close to the accent's own identity as the threshold allows.
 * `lighten(accent, 1)` is opaque white, which always clears 4.5:1 against a fill
 * this dark, so the loop is guaranteed to terminate before exhausting the range;
 * `ink` is an unreachable-in-practice fallback, never expected to fire.
 */
export function secondaryLabelColor(
  accent: string,
  bg: string,
  mode: "light" | "dark",
  ink: string,
): string {
  if (mode === "light") {
    return accent;
  }

  const MIN_CONTRAST = 4.5;
  const STEP = 1 / 64;

  const fill = compositeOver(accent, SECONDARY_FILL_ALPHA, bg);
  if (contrastRatio(accent, fill) >= MIN_CONTRAST) {
    return accent;
  }

  let steps = 1;
  while (steps <= 64) {
    const lifted = lighten(accent, steps * STEP);
    if (contrastRatio(lifted, fill) >= MIN_CONTRAST) {
      return lifted;
    }
    steps += 1;
  }
  return ink;
}
