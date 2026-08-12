/**
 * Chip per DESIGN.md §5: pill, micro type. Variants:
 * neutral (surfaceAlt/inkSecondary), accent (accentSoft/accent),
 * success, danger, warning (soft bg / semantic ink).
 * Used for roles, pledge classes, categories, "Correction" badges.
 */

import { View, type ViewStyle } from "react-native";

import { radii, spacing, useTheme, type Palette } from "@/theme";

import { AppText } from "./AppText";

export type ChipVariant = "neutral" | "accent" | "success" | "danger" | "warning";

function chipColors(palette: Palette, variant: ChipVariant): { bg: string; fg: string } {
  switch (variant) {
    case "neutral":
      return { bg: palette.surfaceAlt, fg: palette.inkSecondary };
    case "accent":
      return { bg: palette.accentSoft, fg: palette.accent };
    case "success":
      return { bg: palette.successSoft, fg: palette.success };
    case "danger":
      return { bg: palette.dangerSoft, fg: palette.danger };
    case "warning":
      return { bg: palette.warningSoft, fg: palette.warning };
  }
}

export interface ChipProps {
  label: string;
  variant?: ChipVariant;
  style?: ViewStyle;
}

export function Chip({ label, variant = "neutral", style }: ChipProps) {
  const palette = useTheme();
  const { bg, fg } = chipColors(palette, variant);

  return (
    <View
      style={[
        {
          alignSelf: "flex-start",
          borderRadius: radii.pill,
          paddingHorizontal: spacing.md,
          paddingVertical: spacing.xs,
          backgroundColor: bg,
        },
        style,
      ]}
    >
      <AppText variant="micro" style={{ color: fg }}>
        {label}
      </AppText>
    </View>
  );
}
