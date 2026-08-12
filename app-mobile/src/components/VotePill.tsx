/**
 * VotePill per DESIGN.md §5: single vertical capsule on Yak cards —
 * chevron-up / score (stat type) / chevron-down in surfaceAlt; the active
 * direction fills accent (up) / danger (down) with a white glyph.
 */

import { Feather } from "@expo/vector-icons";
import { Pressable, View, type ViewStyle } from "react-native";

import { radii, spacing, useTheme } from "@/theme";

import { AppText, type TextTone } from "./AppText";

export type VoteDirection = "up" | "down";

export interface VotePillProps {
  score: number;
  /** Which direction the viewer has voted, if any. */
  vote?: VoteDirection | null;
  onUpvote?: () => void;
  onDownvote?: () => void;
  style?: ViewStyle;
}

function VoteGlyph({
  icon,
  active,
  activeBg,
  onPress,
}: {
  icon: "chevron-up" | "chevron-down";
  active: boolean;
  activeBg: string;
  onPress?: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        borderRadius: radii.pill,
        paddingHorizontal: spacing.sm,
        paddingVertical: spacing.xs,
        backgroundColor: active ? activeBg : "transparent",
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather name={icon} size={16} color={active ? palette.onAccent : palette.inkFaint} />
    </Pressable>
  );
}

export function VotePill({ score, vote = null, onUpvote, onDownvote, style }: VotePillProps) {
  const palette = useTheme();

  const scoreTone: TextTone = vote === "up" ? "accent" : vote === "down" ? "danger" : "primary";

  return (
    <View
      style={[
        {
          alignSelf: "flex-start",
          alignItems: "center",
          borderRadius: radii.pill,
          backgroundColor: palette.surfaceAlt,
          paddingVertical: spacing.xs,
          paddingHorizontal: spacing.xs,
          gap: spacing.xs,
        },
        style,
      ]}
    >
      <VoteGlyph icon="chevron-up" active={vote === "up"} activeBg={palette.accent} onPress={onUpvote} />
      <AppText variant="stat" tone={scoreTone}>
        {score}
      </AppText>
      <VoteGlyph icon="chevron-down" active={vote === "down"} activeBg={palette.danger} onPress={onDownvote} />
    </View>
  );
}
