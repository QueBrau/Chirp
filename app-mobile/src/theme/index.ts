/**
 * Theme barrel: all design tokens plus useTheme(), which resolves the active
 * palette from the system color scheme AND the user's campus appearance prefs
 * (DESIGN §8.5 — see ./appearance.tsx).
 */

import { useColorScheme, type ViewStyle } from "react-native";
import { useMemo } from "react";

import type { Palette } from "./colors";
import { resolvePalette, useAppearance } from "./appearance";

export { brand, dark, light } from "./colors";
export type { GradientPair, Palette } from "./colors";
export { spacing } from "./spacing";
export type { SpacingToken } from "./spacing";
export { typography } from "./typography";
export type { TypeStyle, TypographyVariant } from "./typography";
export { radii } from "./radii";
export type { RadiusToken } from "./radii";
export {
  AppearanceProvider,
  DEFAULT_APPEARANCE_PREFS,
  resolvePalette,
  useAppearance,
} from "./appearance";
export type {
  AccentSource,
  AppearancePrefs,
  AppearanceProviderProps,
  BackgroundStyle,
  CampusColors,
} from "./appearance";
export {
  contrastWithWhite,
  darken,
  ensureAccentContrast,
  hexToRgb,
  lighten,
  mix,
  relativeLuminance,
  withAlpha,
} from "./colorUtils";

/**
 * Elevation presets. `card` is the DESIGN.md §4 spec (0 2px 16px rgba(16,18,35,0.06));
 * light mode only — dark surfaces use border alone (see cardShadow()).
 * `low`/`medium` are legacy v1 presets kept for compatibility.
 */
export const elevation = {
  none: {},
  card: {
    shadowColor: "#101223",
    shadowOpacity: 0.06,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 2 },
    elevation: 3,
  },
  low: {
    shadowColor: "#101223",
    shadowOpacity: 0.06,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 2 },
    elevation: 2,
  },
  medium: {
    shadowColor: "#101223",
    shadowOpacity: 0.1,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 },
    elevation: 6,
  },
} as const satisfies Record<string, ViewStyle>;

export type ElevationToken = keyof typeof elevation;

/** Card shadow per §4: soft shadow in light mode, none in dark (border carries the edge). */
export function cardShadow(palette: Palette): ViewStyle {
  return palette.mode === "dark" ? {} : elevation.card;
}

/**
 * One-off component metrics per DESIGN.md §5 (fixed sizes outside the 4-base scale).
 * Screens/components must reference these instead of hardcoding px.
 */
export const metrics = {
  /** Primary button height (§5). */
  buttonHeight: 52,
  /** EmptyState emoji glyph size (§5). */
  emptyGlyph: 40,
  /** Floating tab bar horizontal inset from screen edges (§5). */
  tabBarInsetX: 12,
  /** Floating tab bar bottom inset (§5). */
  tabBarInsetBottom: 8,
} as const;

/**
 * Bottom padding scrollable tab screens need so content clears the floating
 * tab bar (bar height + bottom inset + breathing room).
 */
export const TAB_BAR_CLEARANCE = 96;

/**
 * Returns the active color palette: system light/dark scheme, resolved through
 * the user's campus appearance prefs (accent source + background style, §8.5).
 * Same return shape as before — every existing screen keeps working unchanged.
 */
export function useTheme(): Palette {
  const mode = useColorScheme() === "dark" ? "dark" : "light";
  const { prefs, campusColors } = useAppearance();
  return useMemo(() => resolvePalette(mode, prefs, campusColors), [mode, prefs, campusColors]);
}
