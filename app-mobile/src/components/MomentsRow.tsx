/**
 * MomentsRow (DESIGN §7 — Snapchat DNA, tile reference: Mostafizur Rahaman
 * dribbble shot): horizontal strip of 64x64 rounded-square tiles (radius 20) —
 * GradientAvatar fill, 2px accent ring inset, name caption under. First tile is
 * always "Your story" (Feather plus in accentSoft). Mock-only taps.
 */

import { Feather } from "@expo/vector-icons";
import { Pressable, ScrollView, View } from "react-native";

import { radii, spacing, typography, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { GradientAvatar } from "./GradientAvatar";

const TILE = 64;
const RING_WIDTH = 2;
/** Gap between the accent ring and the avatar fill, reading as an "inset" ring. */
const RING_GAP = 2;
const FILL_SIZE = TILE - (RING_WIDTH + RING_GAP) * 2;

interface MomentTileProps {
  name: string;
  isAdd?: boolean;
  onPress?: () => void;
}

function MomentTile({ name, isAdd = false, onPress }: MomentTileProps) {
  const palette = useTheme();

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={isAdd ? "Your story" : name}
      onPress={onPress}
      style={({ pressed }) => ({ width: TILE, alignItems: "center", gap: spacing.xs, opacity: pressed ? 0.8 : 1 })}
    >
      <View
        style={{
          width: TILE,
          height: TILE,
          borderRadius: radii.card,
          borderWidth: RING_WIDTH,
          borderColor: palette.accent,
          padding: RING_GAP,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {isAdd ? (
          <View
            style={{
              width: FILL_SIZE,
              height: FILL_SIZE,
              borderRadius: radii.avatar,
              backgroundColor: palette.accentSoft,
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Feather name="plus" size={typography.title.fontSize} color={palette.accent} />
          </View>
        ) : (
          <GradientAvatar name={name} size={FILL_SIZE} />
        )}
      </View>
      <AppText variant="caption" tone="secondary" numberOfLines={1} style={{ width: TILE, textAlign: "center" }}>
        {isAdd ? "Your story" : name}
      </AppText>
    </Pressable>
  );
}

export interface MomentsRowMoment {
  id: string;
  name: string;
}

export interface MomentsRowProps {
  moments: MomentsRowMoment[];
  onPressYourStory?: () => void;
  onPressMoment?: (id: string) => void;
}

export function MomentsRow({ moments, onPressYourStory, onPressMoment }: MomentsRowProps) {
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={{ gap: spacing.md, paddingRight: spacing.gutter }}
    >
      <MomentTile isAdd name="Your story" onPress={onPressYourStory} />
      {moments.map((moment) => (
        <MomentTile key={moment.id} name={moment.name} onPress={() => onPressMoment?.(moment.id)} />
      ))}
    </ScrollView>
  );
}
