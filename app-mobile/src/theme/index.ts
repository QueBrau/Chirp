/**
 * Theme barrel: all design tokens plus useTheme(), which resolves the active
 * palette from the system color scheme AND the user's campus appearance prefs
 * (DESIGN §8.5 — see ./appearance.tsx).
 */

import { useColorScheme, type ViewStyle } from "react-native";
import { useMemo } from "react";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { Palette } from "./colors";
import { resolvePalette, useAppearance } from "./appearance";
import { applyOrgAccent, useOrgAccentColors } from "./orgScope";
import { spacing } from "./spacing";

export { brand, dark, light } from "./colors";
export type { GradientPair, Palette } from "./colors";
export { spacing };
export type { SpacingToken } from "./spacing";
export { typography } from "./typography";
export type { TypeStyle, TypographyVariant } from "./typography";
export { radii } from "./radii";
export type { RadiusToken } from "./radii";
export {
  AppearanceProvider,
  campusNightWash,
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
export { applyOrgAccent, OrgAccentScope, useOrgAccentColors } from "./orgScope";
export type { OrgAccentScopeProps, OrgColors } from "./orgScope";

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
  /**
   * How far the floating tab bar slides down when auto-hiding on scroll.
   * Comfortably past its own height plus the bottom safe-area inset, so it
   * clears the screen edge entirely instead of leaving a sliver visible.
   */
  tabBarHiddenOffset: 140,
  /**
   * Approximate rendered height of the floating tab bar's own box (§5),
   * independent of the safe-area inset it sits above: outer paddingVertical
   * (spacing.sm * 2) + border (1px * 2) + the tallest tab content (an
   * inactive icon plus its own vertical padding) works out to ~54; this
   * rounds up for breathing room. The single shared source for both
   * `useOverlayClearance` below and Fab's own positioning, so a future resize
   * of the bar only needs changing here (c168 — previously Fab.tsx alone
   * approximated this as a local, unexported constant nothing else could see).
   */
  tabBarBoxHeight: 64,
  /** Fab's circle diameter (§7) — shared with `useOverlayClearance` so the
   * clearance a FAB screen reserves always matches the FAB actually rendered. */
  fabSize: 56,
  /**
   * Header accent bar under an oversized screen title (§10.1: "zones, not card
   * soup" — Home/Chirp/Orgs headers get a short accent bar under the title).
   * Dimensions only — color is the screen's own accent (campus primary by
   * default, or campusColors.secondary for the gold moment on Home/Chirp per
   * §10.4), never a fixed hex, so it moves with campus/org theming.
   */
  accentBarWidth: 4,
  accentBarHeight: 28,
  accentBarRadius: 2,
} as const;

/**
 * Bottom padding a scrollable screen needs so its LAST row/card clears the
 * floating overlays (DESIGN.md §5 tab bar, §7 FAB) instead of sitting under
 * them — c168 (found on a real iOS simulator: Secretary's meetings list, the
 * Orgs Tools grid, and feed post text were all clipped by the pill/FAB).
 *
 * This used to be a flat `TAB_BAR_CLEARANCE = 96` constant that baked in an
 * assumed safe-area inset instead of reading the device's real one, and had
 * no FAB-aware variant — Home and the Orgs feed (which also render a sibling
 * `<Fab/>`, floating `spacing.md + fabSize` further above the tab bar) used
 * the exact same number as every screen with no FAB at all. Both cases are
 * derived here from the same constants FloatingTabBar and Fab actually render
 * with (`metrics.tabBarBoxHeight`, `metrics.fabSize`) plus the REAL
 * `useSafeAreaInsets().bottom` for this device, plus one `spacing.lg` of
 * breathing room — never eyeballed, and there is exactly one place to update
 * if the tab bar or FAB ever change size.
 *
 * `Screen` is the only intended call site (via its `hasFab` prop) — pass
 * `hasFab: true` when the screen also renders a sibling `<Fab/>` so its
 * content clears both overlays instead of just the tab bar.
 */
export function useOverlayClearance(hasFab: boolean = false): number {
  const insets = useSafeAreaInsets();
  const tabBarTop = Math.max(insets.bottom, metrics.tabBarInsetBottom) + metrics.tabBarBoxHeight;
  const fabExtra = hasFab ? spacing.md + metrics.fabSize : 0;
  return tabBarTop + fabExtra + spacing.lg;
}

/**
 * Returns the active color palette: system light/dark scheme, resolved through
 * the user's campus appearance prefs (accent source + background style, §8.5),
 * then through the nearest OrgAccentScope if the call site is inside one (§8.6—
 * e.g. anywhere under the Orgs/chapter stack). Same return shape as before —
 * every existing screen keeps working unchanged; org colors only apply where a
 * screen explicitly opts a subtree in via <OrgAccentScope>.
 */
export function useTheme(): Palette {
  const mode = useColorScheme() === "dark" ? "dark" : "light";
  const { prefs, campusColors } = useAppearance();
  const orgColors = useOrgAccentColors();
  return useMemo(() => {
    const palette = resolvePalette(mode, prefs, campusColors);
    return orgColors ? applyOrgAccent(palette, orgColors) : palette;
  }, [mode, prefs, campusColors, orgColors]);
}
