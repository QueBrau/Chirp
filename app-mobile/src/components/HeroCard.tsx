/**
 * HeroCard per DESIGN.md §5: accentGradient background, white text, radius 20.
 * For org identity headers and the treasurer balance. Max ONE per screen.
 *
 * LinearGradient is not installed — the 135deg gradient is faked with a solid
 * start-color base plus translucent end-color circles drifting bottom-right.
 * Put white text inside via <AppText tone="onAccent">.
 */

import type { ReactNode } from "react";
import { Pressable, View, type ViewStyle } from "react-native";

import { cardShadow, radii, spacing, useTheme } from "@/theme";

export interface HeroCardProps {
  children: ReactNode;
  onPress?: () => void;
  style?: ViewStyle;
}

export function HeroCard({ children, onPress, style }: HeroCardProps) {
  const palette = useTheme();
  const [start, end] = palette.accentGradient;

  const base: ViewStyle = {
    borderRadius: radii.card,
    backgroundColor: start,
    overflow: "hidden",
    ...cardShadow(palette),
  };

  const inner = (
    <>
      {/* Fake 135deg gradient: end color pooling toward the bottom-right. */}
      <View
        style={{
          position: "absolute",
          top: "10%",
          left: "35%",
          width: "90%",
          aspectRatio: 1,
          borderRadius: radii.pill,
          backgroundColor: end,
          opacity: 0.45,
        }}
      />
      <View
        style={{
          position: "absolute",
          top: "45%",
          left: "55%",
          width: "80%",
          aspectRatio: 1,
          borderRadius: radii.pill,
          backgroundColor: end,
          opacity: 0.6,
        }}
      />
      <View style={{ padding: spacing.gutter }}>{children}</View>
    </>
  );

  if (onPress) {
    return (
      <Pressable
        accessibilityRole="button"
        onPress={onPress}
        style={({ pressed }) => [base, { opacity: pressed ? 0.9 : 1 }, style]}
      >
        {inner}
      </Pressable>
    );
  }
  return <View style={[base, style]}>{inner}</View>;
}
