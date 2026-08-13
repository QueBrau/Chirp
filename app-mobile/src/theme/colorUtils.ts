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
