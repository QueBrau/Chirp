/**
 * Light + dark color palettes per DESIGN.md §2 ("Campus Modern").
 * The `Palette` interface is the frozen contract screens consume via useTheme().
 * Legacy v1 keys (textPrimary, accentMuted, …) are kept as aliases so older
 * call sites keep compiling; new code should use the v2 names (ink, accentSoft, …).
 */

/** [start, end] endpoint tuple for a 135deg gradient moment (no LinearGradient — fake with layers). */
export type GradientPair = readonly [string, string];

export interface Palette {
  /** Which scheme this palette belongs to (components use it to drop shadows in dark). */
  mode: "light" | "dark";

  // Canvas & surfaces
  bg: string;
  surface: string;
  surfaceAlt: string;

  // Ink
  ink: string;
  inkSecondary: string;
  inkFaint: string;

  border: string;

  // Accent
  accent: string;
  accentSoft: string;
  accentGradient: GradientPair;
  onAccent: string;

  // Semantic
  success: string;
  successSoft: string;
  danger: string;
  dangerSoft: string;
  warning: string;
  warningSoft: string;

  /** Rotating pastel card tints on Yak (index % 4). */
  yakTints: readonly [string, string, string, string];

  /**
   * Categorical chart series (DESIGN §11). Fixed order, assigned in sequence and
   * NEVER cycled — slot order is the colour-blindness safety mechanism, not a
   * preference. Five slots is the cap: a 6th category folds into `chartOther`.
   *
   * Both modes were validated with the dataviz validator (lightness band, chroma
   * floor, CVD separation under protanopia/deuteranopia, normal-vision floor and
   * contrast vs `surface`), including the donut's wrap-around pair, since the last
   * segment touches the first. Do not hand-edit a slot: re-run the validator.
   */
  chartCategorical: readonly [string, string, string, string, string];

  /**
   * The folded "Other" slice. Deliberately a NEUTRAL, not a sixth hue: it is the
   * absence of an identity, and giving it one implies a category that isn't there.
   */
  chartOther: string;

  /** Preset gradient pairs for GradientAvatar (picked by name hash). */
  avatarGradients: readonly GradientPair[];

  // ---- Legacy v1 aliases (do not use in new code) ----
  surfaceRaised: string;
  textPrimary: string;
  textSecondary: string;
  textTertiary: string;
  accentMuted: string;
}

/** Chirp brand indigo — the accentGradient start endpoint. */
export const brand = "#6366F1";

/** Shared gradient endpoints (§2: identical in both modes). */
const accentGradient: GradientPair = ["#6366F1", "#8B5CF6"];

/**
 * Five preset avatar gradient pairs (DESIGN.md §5), built exclusively from §2
 * endpoint/semantic colors — no colors outside the contract.
 */
const avatarGradients: readonly GradientPair[] = [
  ["#6366F1", "#8B5CF6"], // indigo → violet (brand)
  ["#5B5BF6", "#8B5CF6"], // accent → violet
  ["#17A673", "#6366F1"], // green → indigo
  ["#F5A623", "#E5484D"], // amber → coral
  ["#E5484D", "#8B5CF6"], // coral → violet
];

export const light: Palette = {
  mode: "light",

  bg: "#F6F7FB",
  surface: "#FFFFFF",
  surfaceAlt: "#EFF1F7",

  ink: "#101223",
  inkSecondary: "#575B75",
  inkFaint: "#9BA0B8",

  border: "rgba(16,18,35,0.08)",

  accent: "#5B5BF6",
  accentSoft: "#ECECFE",
  accentGradient,
  onAccent: "#FFFFFF",

  success: "#17A673",
  successSoft: "#E3F6EE",
  danger: "#E5484D",
  dangerSoft: "#FDECEC",
  warning: "#F5A623",
  warningSoft: "#FDF3E1",

  yakTints: ["#FFF3E9", "#EDF6FF", "#F3EDFF", "#EAF8F1"],

  // Validated on surface #FFFFFF: worst adjacent CVD dE 13.8, normal-vision 28.8,
  // all >= 3:1 contrast. Slot order chosen by enumerating all 120 permutations and
  // taking the one with the best worst-case adjacent separation.
  chartCategorical: ["#5B5BF6", "#DB2777", "#0284C7", "#EA580C", "#0D9488"],
  chartOther: "#9BA0B8",

  avatarGradients,

  // Legacy aliases
  surfaceRaised: "#FFFFFF",
  textPrimary: "#101223",
  textSecondary: "#575B75",
  textTertiary: "#9BA0B8",
  accentMuted: "#ECECFE",
};

export const dark: Palette = {
  mode: "dark",

  bg: "#0C0D14",
  surface: "#15161F",
  surfaceAlt: "#1D1E2A",

  ink: "#F2F3FA",
  inkSecondary: "#A6AAC4",
  inkFaint: "#666B85",

  border: "rgba(242,243,250,0.09)",

  accent: "#7C7CFF",
  accentSoft: "rgba(124,124,255,0.16)",
  accentGradient,
  onAccent: "#FFFFFF",

  success: "#2BD597",
  successSoft: "rgba(43,213,151,0.16)",
  danger: "#FF6369",
  dangerSoft: "rgba(255,99,105,0.16)",
  warning: "#FFB84D",
  warningSoft: "rgba(255,184,77,0.16)",

  yakTints: [
    "rgba(255,243,233,0.12)",
    "rgba(237,246,255,0.12)",
    "rgba(243,237,255,0.12)",
    "rgba(234,248,241,0.12)",
  ],

  // Its own steps, not a flip of the light ramp: validated on surface #15161F,
  // worst adjacent CVD dE 8.1, normal-vision 24.4, all >= 3:1. The light steps sit
  // outside the dark lightness band (0.48-0.67), which is why these differ.
  chartCategorical: ["#7C7CFF", "#EC4899", "#0891B2", "#EA580C", "#10A99A"],
  chartOther: "#666B85",

  avatarGradients,

  // Legacy aliases
  surfaceRaised: "#1D1E2A",
  textPrimary: "#F2F3FA",
  textSecondary: "#A6AAC4",
  textTertiary: "#666B85",
  accentMuted: "rgba(124,124,255,0.16)",
};
