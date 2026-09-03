/**
 * AvatarStack (DESIGN §8.7): overlapping photo-avatar stack for an event's RSVP
 * summary — "N going" at a glance on the event card. Each avatar gets a
 * surface-colored ring so it reads as a stack rather than a smear; overflow
 * collapses into a trailing "+N" pill.
 */

import { View } from "react-native";

import { radii, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { GradientAvatar } from "./GradientAvatar";

export interface AvatarStackPerson {
  name: string;
  photoUrl?: string | null;
}

export interface AvatarStackProps {
  people: AvatarStackPerson[];
  /** Avatar diameter. Default 24. */
  size?: number;
  /** Max avatars shown before collapsing the rest into a "+N" pill. Default 4. */
  max?: number;
  /** True crowd size when `people` is only a preview (c280); drives the "+N" pill.
   * Defaults to people.length, i.e. the pre-c280 behavior. */
  total?: number;
}

export function AvatarStack({ people, size = 24, max = 4, total }: AvatarStackProps) {
  const palette = useTheme();
  const shown = people.slice(0, max);
  // total lets a caller pass a PREVIEW of a larger crowd (c280): the +N bubble then
  // reads the true count, not the preview's length. Omitted, behavior is unchanged.
  const overflow = Math.max(0, (total ?? people.length) - shown.length);
  const overlap = size * 0.4;

  return (
    <View style={{ flexDirection: "row", alignItems: "center" }}>
      {shown.map((person, index) => (
        <View
          key={`${person.name}-${index}`}
          style={{
            marginLeft: index === 0 ? 0 : -overlap,
            borderRadius: radii.pill,
            borderWidth: 2,
            borderColor: palette.surface,
          }}
        >
          <GradientAvatar name={person.name} size={size} photoUrl={person.photoUrl} />
        </View>
      ))}
      {overflow > 0 ? (
        <View
          style={{
            marginLeft: -overlap,
            width: size,
            height: size,
            borderRadius: radii.pill,
            borderWidth: 2,
            borderColor: palette.surface,
            backgroundColor: palette.surfaceAlt,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <AppText variant="caption" tone="secondary" style={{ fontSize: size * 0.4 }}>
            +{overflow}
          </AppText>
        </View>
      ) : null}
    </View>
  );
}
