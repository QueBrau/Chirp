/**
 * Org accent scope (DESIGN.md §8.6): every fraternity/sorority carries its own
 * `colors: { primary, secondary }`, and the ENTIRE Orgs stack (chapter/* screens)
 * should wear that org's colors — hero, tool tiles, chips, active states — while
 * campus colors keep applying everywhere else. <OrgAccentScope> is how a screen
 * opts a subtree into that: it overrides accent/accentSoft/onAccent/accentGradient
 * on whatever useTheme() (./index.ts) resolves inside it.
 *
 * Composes with (does not replace) AppearanceProvider (./appearance.tsx):
 * useTheme() resolves the campus/user palette first via resolvePalette(), then
 * this scope's override is layered on top when the call site sits inside one.
 * Same contrast guard as campus accent (§8.5) applies to org colors (§8.6).
 */

import { createContext, useContext, useMemo, type ReactNode } from "react";

import type { GradientPair, Palette } from "./colors";
import { ensureAccentContrast, withAlpha } from "./colorUtils";

/** An org's brand colors. Same shape as CampusColors (appearance.tsx) but kept as
 * its own type — org identity and campus identity are independent axes (§8.6). */
export interface OrgColors {
  primary: string;
  secondary: string;
}

const OrgAccentContext = createContext<OrgColors | null>(null);

export interface OrgAccentScopeProps extends OrgColors {
  children: ReactNode;
}

/**
 * Wraps a subtree (e.g. the whole `chapter/*` route stack) so every useTheme()
 * call inside sees `{primary, secondary}` as that org's accent/accentSoft/
 * onAccent/accentGradient, instead of the user's campus/Chirp accent choice.
 * Nest freely — the nearest scope wins.
 */
export function OrgAccentScope({ primary, secondary, children }: OrgAccentScopeProps) {
  const value = useMemo<OrgColors>(() => ({ primary, secondary }), [primary, secondary]);
  return <OrgAccentContext.Provider value={value}>{children}</OrgAccentContext.Provider>;
}

/** Reads the nearest OrgAccentScope, or null outside one. Used internally by useTheme(); exported for previews/tests. */
export function useOrgAccentColors(): OrgColors | null {
  return useContext(OrgAccentContext);
}

/**
 * Applies an org's colors on top of an already-resolved Palette (post campus/user
 * appearance resolution — see resolvePalette in ./appearance.tsx). The org's
 * primary goes through the same 3:1-against-white contrast guard as campus accent
 * (§8.5's ensureAccentContrast). accentGradient becomes the org's own
 * [primary, secondary] pair — a real two-tone brand gradient, distinct from the
 * campus scope's [accent, lightened-accent] pattern, because an org's two colors
 * ARE its identity (Sigma Chi blue→gold reads as Sigma Chi; a single-hue gradient
 * wouldn't).
 */
export function applyOrgAccent(palette: Palette, org: OrgColors): Palette {
  const accent = ensureAccentContrast(org.primary);
  const accentSoft = palette.mode === "dark" ? withAlpha(accent, 0.16) : withAlpha(accent, 0.15);
  const accentGradient: GradientPair = [accent, org.secondary];

  return {
    ...palette,
    accent,
    accentSoft,
    accentGradient,
    onAccent: "#FFFFFF",
    accentMuted: accentSoft, // legacy alias stays in sync with the derived accentSoft
  };
}
