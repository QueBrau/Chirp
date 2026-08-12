/** Pill button primitive: primary / secondary / ghost / danger variants from theme tokens. */

import { Pressable, StyleSheet, type ViewStyle } from "react-native";

import { radii, spacing, useTheme } from "@/theme";

import { AppText, type TextTone } from "./AppText";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

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
      container.backgroundColor = palette.surface;
      container.borderWidth = StyleSheet.hairlineWidth;
      container.borderColor = palette.border;
      tone = "primary";
      break;
    case "ghost":
      container.backgroundColor = "transparent";
      tone = "accent";
      break;
    case "danger":
      container.backgroundColor = palette.danger;
      tone = "onAccent";
      break;
  }

  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [container, { opacity: disabled ? 0.5 : pressed ? 0.8 : 1 }, style]}
    >
      <AppText tone={tone} style={{ fontWeight: "600" }}>
        {label}
      </AppText>
    </Pressable>
  );
}
