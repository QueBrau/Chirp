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
 * concept of alpha, so passing an rgba() token (accentSoft, the dark-mode border
 * tokens) silently measures nonsense rather than throwing. Composite first
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
 * The `secondary` fill's alpha, split per mode (c386 dark, c397 light). Matches
 * `accentSoft`'s own alpha split in appearance.tsx/orgScope.tsx (0.16 dark / 0.15
 * light) -- duplicated there rather than imported because those two derivation
 * sites predate this card and already had their own literals before
 * secondaryLabelColor existed; not touched here to keep this card's diff to the
 * contrast fix. Exported so at least the verify script (below) and
 * secondaryLabelColor's own fill computation share one source instead of two.
 * Two constants, not one, because the dark and light values differ (0.16 vs
 * 0.15) and a single shared constant would force one mode to use the other's
 * alpha.
 */
export const SECONDARY_FILL_ALPHA_DARK = 0.16;
export const SECONDARY_FILL_ALPHA_LIGHT = 0.15;

/**
 * Board c386 (dark mode), extended to light mode by c397. `secondary` buttons
 * fill with `accentSoft` (accent alpha-composited at 16% dark / 15% light over
 * `bg`, per appearance.tsx/orgScope.tsx) and label with the accent, adjusted just
 * enough to stay legible against that fill. That reads fine whenever the accent
 * itself is light (dark mode) or dark (light mode) enough to clear AA against its
 * own faint wash -- it does NOT for every accent: UNCG navy on its own dark fill
 * measures ~1.2:1 (dark mode), and the default violet / Alpha Delta Pi's azure
 * measure ~3.79:1 / ~2.49:1 on their own light fill (light mode) -- both below
 * the 4.5:1 AA threshold.
 *
 * Both branches now share the same shape, mirrored around the mode's own
 * direction of travel: composite the real fill the button actually paints (using
 * the caller's live accent/bg, so campus tint and org-scoped accents are honored
 * automatically); if the raw accent already clears 4.5:1 against that fill,
 * return it UNCHANGED -- an already-legible accent (e.g. the default violet in
 * dark mode, or UNCG navy / Sigma Chi in light mode) must not be silently
 * relabeled. Otherwise adjust the accent in fine (1/64) steps -- LIGHTENED
 * (toward white) in dark mode, DARKENED (toward black) in light mode -- and
 * return the FIRST step whose contrast against the fill clears 4.5:1, so the
 * result stays as close to the accent's own identity (and hue) as the threshold
 * allows. `lighten(accent, 1)` is opaque white and `darken(accent, 1)` is opaque
 * black, both of which always clear 4.5:1 against a fill this far from them, so
 * each loop is guaranteed to terminate before exhausting its range; `ink` is an
 * unreachable-in-practice fallback in both branches, never expected to fire.
 *
 * The dark branch's behavior is unchanged from c386: same alpha (0.16, now named
 * SECONDARY_FILL_ALPHA_DARK instead of the old single SECONDARY_FILL_ALPHA),
 * same threshold, same step, same lighten() direction, same output for every
 * input -- pinned byte-identical in verify-button-contrast.mjs.
 */
export function secondaryLabelColor(
  accent: string,
  bg: string,
  mode: "light" | "dark",
  ink: string,
): string {
  const MIN_CONTRAST = 4.5;
  const STEP = 1 / 64;

  if (mode === "light") {
    const fill = compositeOver(accent, SECONDARY_FILL_ALPHA_LIGHT, bg);
    if (contrastRatio(accent, fill) >= MIN_CONTRAST) {
      return accent;
    }

    let steps = 1;
    while (steps <= 64) {
      const darkened = darken(accent, steps * STEP);
      if (contrastRatio(darkened, fill) >= MIN_CONTRAST) {
        return darkened;
      }
      steps += 1;
    }
    return ink;
  }

  const fill = compositeOver(accent, SECONDARY_FILL_ALPHA_DARK, bg);
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
