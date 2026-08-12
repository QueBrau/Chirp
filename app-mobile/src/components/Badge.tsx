/** Small pill badge: accent fill for emphasis, outline styles for semantic tones. */

import { View } from "react-native";

import { radii, spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";

export type BadgeTone = "accent" | "success" | "danger" | "neutral";

export interface BadgeProps {
  label: string;
  tone?: BadgeTone;
}

export function Badge({ label, tone = "neutral" }: BadgeProps) {
  const palette = useTheme();

  const filled = tone === "accent";
  const textColor =
    tone === "accent"
      ? palette.accent
      : tone === "success"
        ? palette.success
        : tone === "danger"
          ? palette.danger
          : palette.textSecondary;

  return (
    <View
      style={{
        alignSelf: "flex-start",
        borderRadius: radii.pill,
        paddingHorizontal: spacing.sm,
        paddingVertical: spacing.xs / 2,
        backgroundColor: filled ? palette.accentMuted : "transparent",
        borderWidth: filled ? 0 : 1,
        borderColor: textColor,
      }}
    >
      <AppText variant="caption" style={{ color: textColor, fontWeight: "600" }}>
        {label}
      </AppText>
    </View>
  );
}
