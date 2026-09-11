/**
 * Theme barrel: all design tokens plus useTheme(), which resolves the active
 * palette from the system color scheme AND the user's campus appearance prefs
 * (DESIGN §8.5 — see ./appearance.tsx).
 */

import { useColorScheme, type TextStyle, type ViewStyle } from "react-native";
import { useMemo } from "react";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { Palette } from "./colors";
import { resolvePalette, useAppearance } from "./appearance";
import type { CampusColors } from "./appearance";
import { contrastRatio, withAlpha } from "./colorUtils";
import { applyOrgAccent, useOrgAccentColors } from "./orgScope";
import { radii } from "./radii";
import { spacing } from "./spacing";
import { typography } from "./typography";

export { brand, dark, light } from "./colors";
export type { GradientPair, Palette } from "./colors";
export { spacing };
export type { SpacingToken } from "./spacing";
export { typography };
export type { TypeStyle, TypographyVariant } from "./typography";
export { radii };
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
  compositeOver,
  contrastRatio,
  contrastWithWhite,
  darken,
  ensureAccentContrast,
  hexToRgb,
  lighten,
  mix,
  relativeLuminance,
  secondaryLabelColor,
  SECONDARY_FILL_ALPHA_DARK,
  SECONDARY_FILL_ALPHA_LIGHT,
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
 * Link/action TEXT colour on a bare canvas — text with no button behind it, like
 * "Forgot password?" or the sign-in/sign-up footer toggle (c385).
 *
 * WHY THIS IS NOT SIMPLY `palette.accent`. The default accent source is CAMPUS
 * PRIMARY (§8.5), and UNCG's is a navy so dark that on the DARK canvas it measures
 * about 1.2:1 — an invisible link. `ensureAccentContrast` does not catch this and is
 * not meant to: it guards the accent against WHITE, for accent-as-a-fill-under-
 * white-text, which is the opposite arrangement to accent-as-text-on-the-canvas.
 *
 * So this MEASURES rather than assumes, and the order is a fallback chain, not a
 * light/dark switch: accent when it clears AA for body text, else the campus
 * SECONDARY (the §10.4 gold moment, bright and therefore legible exactly where a
 * dark accent fails), else ink, which always clears. A mode switch would be wrong
 * for the Chirp-violet accent source, which is perfectly legible in dark and would
 * be needlessly replaced by gold.
 *
 * 4.5:1 rather than 3:1 because these are `caption`-sized links: body text by
 * WCAG's reckoning, not large text.
 */
export function canvasActionColor(palette: Palette, campusColors: CampusColors): string {
  const AA_BODY_TEXT = 4.5;
  if (contrastRatio(palette.accent, palette.bg) >= AA_BODY_TEXT) return palette.accent;
  if (contrastRatio(campusColors.secondary, palette.bg) >= AA_BODY_TEXT) return campusColors.secondary;
  return palette.ink;
}

/**
 * Background for a small control sitting ON a post card — today the circular
 * overflow button in a card header (§5 PostCard).
 *
 * `surfaceAlt` in BOTH modes, which only became the right answer when c384 flattened
 * the cards. While cards were tinted this returned `surface`: a white circle on a
 * pastel card in light, and an ink wash in dark because the 12% tints sat LIGHTER
 * than `surface` and a surface-colored circle would have sunk into the card. Both
 * halves of that reasoning die with the tints - a `surface` circle on a card that is
 * now itself `surface` is INVISIBLE in light mode, which is the bug this rename
 * exists to make un-writable.
 *
 * surfaceAlt is the token whose whole job is "one step off surface", and it steps the
 * correct DIRECTION in each mode without a mode switch: lighter than the card in dark
 * (#1D1E2A on #15161F, distance 15.8), darker than it in light (#EFF1F7 on #FFFFFF,
 * distance 22.7). It is also already the input background, so a control and a text
 * field on the same card now agree instead of being two different near-whites.
 */
export function onCardControl(palette: Palette): string {
  return palette.surfaceAlt;
}

/**
 * The one text-field surface: body type on `surfaceAlt` (DESIGN §2 names it the
 * input bg) at the §4 input radius. Same role as cardShadow() above — a token
 * combination every input needs, in one place, so changing the input treatment
 * moves all of them together.
 *
 * Spread it FIRST and put per-field extras after, exactly as the call sites
 * already ordered them: `{ ...inputField(palette), minHeight: 96 }`. Overriding
 * a value it sets is a smell, not a feature — the two `paddingVertical:
 * spacing.sm` compact fields (president.tsx, dues-plans.tsx) deliberately do
 * NOT use this and keep their own literal block, because routing them through
 * here would cost more lines than it saves and hide that they are different.
 */
export function inputField(palette: Palette): TextStyle {
  return {
    ...typography.body,
    color: palette.ink,
    backgroundColor: palette.surfaceAlt,
    borderRadius: radii.input,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  };
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
   * The circular soft control in a PostCard's header row (§5, c383) — the
   * overflow button on a feed card and on a chirp. Shared rather than written twice
   * because those two cards are supposed to look like the same card, and the Chirps
   * board is hand-rolled: it is exactly the kind of pair that drifts by one edit.
   * Its background comes from `onCardControl(palette)` above.
   */
  cardControlSize: 32,
  /**
   * Header accent bar LEADING an oversized screen title (§10.1: "zones, not card
   * soup"). Dimensions only — color is the screen's own accent (campus primary by
   * default, or campusColors.secondary for the gold moment on Home/Chirp per
   * §10.4), never a fixed hex, so it moves with campus/org theming.
   *
   * NO HEIGHT HERE ON PURPOSE. The bar sits beside the title and takes the title's
   * own height via `alignSelf: "stretch"`, so it stays aligned if the display type
   * scale ever moves. The old fixed 28 was removed rather than left unused: kept
   * around, the next person to hand-roll this header would have reached for it and
   * quietly reintroduced a bar that no longer matches the text beside it.
   */
  accentBarWidth: 4,
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
 * `Screen` is the only intended call site — it passes `hasFab` when the screen
 * renders a sibling `<Fab/>`, and `hasTabBar` derived from the route.
 *
 * hasTabBar EXISTS BECAUSE THIS USED TO RESERVE TAB-BAR SPACE ON SCREENS WITH NO
 * TAB BAR (c385, braul: "too much white space on the bottom of the sign in page").
 * The whole `(auth)` group — sign-in, account-type, join-chapter, verify-campus,
 * suspended — is a plain Stack with no floating bar, and every one of them was
 * padding the bottom by `insets.bottom + 64 + 16`. On an iPhone 15 Pro that is
 * 114pt of dead space under the last control, and the failure is SILENT: nothing
 * looks broken, the page just sits oddly high, which is why it survived until
 * someone looked at it on a phone and said so.
 *
 * DERIVED FROM THE ROUTE RATHER THAN PASSED AS A PROP, deliberately. A
 * `hasTabBar={false}` prop would have to be remembered by every future auth or
 * onboarding screen, and forgetting it reproduces exactly this bug with no
 * symptom to notice. The router already knows the answer.
 */
export function useOverlayClearance(hasFab: boolean = false, hasTabBar: boolean = true): number {
  const insets = useSafeAreaInsets();
  // Without a tab bar the only thing to clear is the home indicator, which
  // SafeAreaView does NOT cover here (Screen takes edges={["top"]} only).
  const overlayTop = hasTabBar
    ? Math.max(insets.bottom, metrics.tabBarInsetBottom) + metrics.tabBarBoxHeight
    : insets.bottom;
  const fabExtra = hasFab ? spacing.md + metrics.fabSize : 0;
  return overlayTop + fabExtra + spacing.lg;
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
