/** Surface card per DESIGN.md §4: radius 20, 1px border token, soft shadow (light mode only). */

import type { ReactNode } from "react";
import { Pressable, View, type ViewStyle } from "react-native";

import { cardShadow, radii, spacing, useTheme } from "@/theme";

export interface CardProps {
  children: ReactNode;
  onPress?: () => void;
  style?: ViewStyle;
}

export function Card({ children, onPress, style }: CardProps) {
  const palette = useTheme();

  const base: ViewStyle = {
    backgroundColor: palette.surface,
    borderRadius: radii.card,
    padding: spacing.lg,
    borderWidth: 1,
    borderColor: palette.border,
    ...cardShadow(palette),
  };

  if (onPress) {
    return (
      <Pressable
        accessibilityRole="button"
        onPress={onPress}
        style={({ pressed }) => [base, { opacity: pressed ? 0.85 : 1 }, style]}
      >
        {children}
      </Pressable>
    );
  }
  return <View style={[base, style]}>{children}</View>;
}
