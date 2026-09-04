/**
 * VotePill per DESIGN.md §5: single vertical capsule on Chirps cards —
 * chevron-up / score (stat type) / chevron-down in surfaceAlt; the active
 * direction fills accent (up) / danger (down) with a white glyph.
 *
 * §10 rule 6 ("numbers have personality"): Chirp overrides the up-vote fill and
 * the score color to campus gold via `upColor`/`scoreColor` — optional, so
 * every other call site keeps the default accent/danger behavior untouched.
 */

import { Feather } from "@expo/vector-icons";
import { Pressable, View, type ViewStyle } from "react-native";

import { type Palette, radii, spacing, useTheme } from "@/theme";

import { AppText, type TextTone } from "./AppText";

export type VoteDirection = "up" | "down";

export interface VotePillProps {
  score: number;
  /** Which direction the viewer has voted, if any. */
  vote?: VoteDirection | null;
  onUpvote?: () => void;
  onDownvote?: () => void;
  /** Overrides the active "up" fill + score color (default `palette.accent`) — Chirp's gold moment. */
  upColor?: string;
  /** Overrides the score color when the viewer hasn't voted (default "primary") — e.g. Chirp's top score. */
  scoreColor?: string;
  /**
   * Pin every color to a specific palette instead of the live (system-following) one.
   *
   * c297, and it was launch-blocking. Chirps cards are light-tinted in BOTH schemes
   * (chirps/index.tsx's header explains why), so everything drawn on them is pinned to
   * the `light` palette — except this component, which kept calling useTheme(). In
   * system dark mode the score resolved to dark.ink (#F2F3FA, near-white) on a
   * near-white pastel card and simply vanished; the only chirp that stayed readable was
   * the single top scorer, because `scoreColor` happened to force gold there. That one
   * visible number is exactly why it survived review.
   *
   * Optional and defaulting to useTheme(), so every other call site is untouched.
   */
  palette?: Palette;
  style?: ViewStyle;
}

function VoteGlyph({
  icon,
  active,
  activeBg,
  onPress,
  palette,
  accessibilityLabel,
}: {
  icon: "chevron-up" | "chevron-down";
  active: boolean;
  activeBg: string;
  onPress?: () => void;
  palette: Palette;
  /** c297: without this a screen reader announces the app's core voting control as
   *  a bare "button" — the role was set, the name never was. */
  accessibilityLabel: string;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
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

export function VotePill({
  score,
  vote = null,
  onUpvote,
  onDownvote,
  upColor,
  scoreColor,
  palette: pinnedPalette,
  style,
}: VotePillProps) {
  const livePalette = useTheme();
  const palette = pinnedPalette ?? livePalette;
  const upFill = upColor ?? palette.accent;

  const scoreTone: TextTone = vote === "down" ? "danger" : "primary";
  // vote "up" always follows upFill (accent by default, gold on Chirps); otherwise
  // an unvoted notable score (e.g. Chirp's top chirp) can still be called out gold.
  const scoreOverride = vote === "up" ? upFill : vote === null ? scoreColor : undefined;
  // Resolved HERE rather than left to AppText's `tone`, because tone is looked up
  // against the LIVE theme inside AppText — which is the exact path that made the score
  // invisible. Identical output to the old tone lookup when no palette is pinned
  // (primary -> ink, danger -> danger), so no other call site changes.
  const resolvedScoreColor =
    scoreOverride ?? (scoreTone === "danger" ? palette.danger : palette.ink);

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
      <VoteGlyph
        icon="chevron-up"
        active={vote === "up"}
        activeBg={upFill}
        onPress={onUpvote}
        palette={palette}
        accessibilityLabel="Upvote"
      />
      <AppText variant="stat" style={{ color: resolvedScoreColor }}>
        {score}
      </AppText>
      <VoteGlyph
        icon="chevron-down"
        active={vote === "down"}
        activeBg={palette.danger}
        onPress={onDownvote}
        palette={palette}
        accessibilityLabel="Downvote"
      />
    </View>
  );
}
