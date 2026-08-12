/** Light + dark color palettes per DESIGN.md; keys are the frozen `Palette` contract. */

export interface Palette {
  bg: string;
  surface: string;
  surfaceRaised: string;
  textPrimary: string;
  textSecondary: string;
  textTertiary: string;
  border: string;
  accent: string;
  accentMuted: string;
  onAccent: string;
  danger: string;
  success: string;
}

/** Chirp Indigo — the brand color (DESIGN.md "Brand"). */
export const brand = "#6366F1";

export const light: Palette = {
  bg: "#FAFAFC",
  surface: "#FFFFFF",
  surfaceRaised: "#FFFFFF",
  textPrimary: "#17171C",
  textSecondary: "#55555E",
  textTertiary: "#8A8A94",
  border: "#E4E4EA",
  accent: "#6366F1",
  accentMuted: "#EEEFFE",
  onAccent: "#FFFFFF",
  danger: "#DC2626",
  success: "#16A34A",
};

export const dark: Palette = {
  bg: "#0F0F14",
  surface: "#1A1A22",
  surfaceRaised: "#23232D",
  textPrimary: "#F2F2F6",
  textSecondary: "#B4B4C0",
  textTertiary: "#7C7C88",
  border: "#2E2E3A",
  accent: "#818CF8",
  accentMuted: "#26284A",
  onAccent: "#0F0F14",
  danger: "#F87171",
  success: "#4ADE80",
};
