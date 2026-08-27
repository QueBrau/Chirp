/**
 * Campus theming (DESIGN.md §8.5): the app's accent + background derive from the
 * user's campus colors plus their own choice, not a fixed palette. AppearanceProvider
 * holds that choice (mock persistence: React state only, seeded by whoever mounts
 * it — see app/_layout.tsx); useTheme() (./index.ts) resolves through this context
 * so every screen already consuming useTheme() reacts to a change instantly.
 */

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { brand, dark, light, type GradientPair, type Palette } from "./colors";
import { darken, ensureAccentContrast, lighten, mix, withAlpha } from "./colorUtils";

/** User's accent choice — which color becomes the `accent` token app-wide. */
export type AccentSource = "campusPrimary" | "campusSecondary" | "chirp";

/** User's background choice — neutral canvas or a wash of their campus primary. */
export type BackgroundStyle = "system" | "campusTint";

export interface AppearancePrefs {
  accentSource: AccentSource;
  backgroundStyle: BackgroundStyle;
}

/** A campus's brand colors (DESIGN §8.5: every campus carries `colors: { primary, secondary }`). */
export interface CampusColors {
  primary: string;
  secondary: string;
}

/**
 * Default prefs per §8.5: campus primary accent + campus tint background — the
 * app should FEEL like the user's school out of the box, not neutral-by-default.
 */
export const DEFAULT_APPEARANCE_PREFS: AppearancePrefs = {
  accentSource: "campusPrimary",
  backgroundStyle: "campusTint",
};

/** Used only when no campus data is available (e.g. a component rendered outside the provider). */
const FALLBACK_CAMPUS_COLORS: CampusColors = { primary: brand, secondary: brand };

interface AppearanceContextValue {
  prefs: AppearancePrefs;
  setPrefs: (update: AppearancePrefs | ((prev: AppearancePrefs) => AppearancePrefs)) => void;
  campusColors: CampusColors;
}

const AppearanceContext = createContext<AppearanceContextValue | null>(null);

export interface AppearanceProviderProps {
  children: ReactNode;
  /** The signed-in user's campus colors. Defaults to Chirp brand violet if omitted. */
  campusColors?: CampusColors;
  /** Seed prefs (mock persistence lives in this component's state for now). */
  initialPrefs?: AppearancePrefs;
}

/** Wraps the app; owns the live appearance prefs so any descendant's useTheme() updates instantly. */
export function AppearanceProvider({
  children,
  campusColors = FALLBACK_CAMPUS_COLORS,
  initialPrefs = DEFAULT_APPEARANCE_PREFS,
}: AppearanceProviderProps) {
  const [prefs, setPrefs] = useState<AppearancePrefs>(initialPrefs);

  const value = useMemo<AppearanceContextValue>(
    () => ({ prefs, setPrefs, campusColors }),
    [prefs, campusColors],
  );

  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

/** Reads + updates the user's appearance prefs. Falls back to sane defaults outside a provider. */
export function useAppearance(): AppearanceContextValue {
  const ctx = useContext(AppearanceContext);
  if (ctx !== null) return ctx;
  return { prefs: DEFAULT_APPEARANCE_PREFS, setPrefs: () => {}, campusColors: FALLBACK_CAMPUS_COLORS };
}

function resolveAccentBase(prefs: AppearancePrefs, campusColors: CampusColors, base: Palette): string {
  switch (prefs.accentSource) {
    case "campusPrimary":
      return campusColors.primary;
    case "campusSecondary":
      return campusColors.secondary;
    case "chirp":
      return base.accent;
  }
}

/**
 * Resolves the active Palette from the base v2 palette (colors.ts) plus the
 * user's appearance prefs and campus colors (DESIGN §8.5). A pure function so
 * it's trivial to preview each option's derived color and so useTheme() stays a
 * thin memoized wrapper around it.
 */
export function resolvePalette(
  mode: "light" | "dark",
  prefs: AppearancePrefs,
  campusColors: CampusColors,
): Palette {
  const base = mode === "dark" ? dark : light;

  const rawAccent = resolveAccentBase(prefs, campusColors, base);
  // Contrast guard (§8.5): darken anything too light for white button/chip text.
  const accent = ensureAccentContrast(rawAccent);
  const accentSoft = mode === "dark" ? withAlpha(accent, 0.16) : withAlpha(accent, 0.15);
  const accentGradient: GradientPair =
    prefs.accentSource === "chirp" ? base.accentGradient : [accent, lighten(accent, 0.18)];

  let bg = base.bg;
  if (prefs.backgroundStyle === "campusTint") {
    // Cards/surfaces stay neutral (§8.5) — only the canvas gets the campus wash.
    const tintWeight = mode === "dark" ? 0.1 : 0.06;
    bg = mix(base.bg, campusColors.primary, tintWeight);
  }

  return {
    ...base,
    accent,
    accentSoft,
    accentGradient,
    accentMuted: accentSoft, // legacy alias stays in sync with the derived accentSoft
    bg,
  };
}

/**
 * The Chirps "campus at night" canvas (DESIGN §10.5). The Chirps board renders on a deep
 * dark wash of the user's campus PRIMARY color as its full-bleed background, the
 * SAME value in both light and dark mode — Chirp deliberately ignores system scheme
 * so it always feels like the campus at night, instantly distinct from Home.
 * Darkens toward black (rather than mixing toward a neutral bg like the §8.5
 * background-tint does) so the campus's own hue survives — UNCG's navy primary
 * stays navy, just deeper; a lighter campus primary still lands on a legible
 * dark canvas. Light tinted cards (palette.chirpTints) float on top of this.
 */
export function campusNightWash(campusColors: CampusColors): string {
  return darken(campusColors.primary, 0.55);
}
