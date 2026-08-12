/**
 * Pill button per DESIGN.md §5 — 52 tall. Variants:
 * primary (accent bg / white), secondary (accentSoft / accent),
 * ghost (transparent / inkSecondary), destructive (dangerSoft / danger).
 * "danger" is a legacy alias for destructive.
 */

import { Pressable, type ViewStyle } from "react-native";

import { metrics, radii, spacing, useTheme } from "@/theme";

import { AppText, type TextTone } from "./AppText";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "destructive" | "danger";

export interface ButtonProps {
  label: string;
  onPress?: () => void;
  variant?: ButtonVariant;
  disabled?: boolean;
  style?: ViewStyle;
}

export function Button({ label, onPress, variant = "primary", disabled = false, style }: ButtonProps) {
  const palette = useTheme();

  const container: ViewStyle = {
    minHeight: metrics.buttonHeight,
    borderRadius: radii.pill,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.xl,
    alignItems: "center",
    justifyContent: "center",
  };
  let tone: TextTone = "onAccent";

  switch (variant) {
    case "primary":
      container.backgroundColor = palette.accent;
      tone = "onAccent";
      break;
    case "secondary":
      container.backgroundColor = palette.accentSoft;
      tone = "accent";
      break;
    case "ghost":
      container.backgroundColor = "transparent";
      tone = "secondary";
      break;
    case "destructive":
    case "danger":
      container.backgroundColor = palette.dangerSoft;
      tone = "danger";
      break;
  }

  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [container, { opacity: disabled ? 0.5 : pressed ? 0.8 : 1 }, style]}
    >
      <AppText variant="bodyBold" tone={tone}>
        {label}
      </AppText>
    </Pressable>
  );
}
