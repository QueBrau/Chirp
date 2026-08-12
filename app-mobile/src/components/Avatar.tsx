/** Initials avatar (no remote images in the scaffold); ghost members render dashed + muted. */

import { View } from "react-native";

import { radii, useTheme } from "@/theme";

import { AppText } from "./AppText";

export interface AvatarProps {
  name: string;
  /** Diameter in units of the type scale — sized via multiplier, default 40. */
  size?: number;
  /** Placeholder historical member (users.is_ghost). */
  ghost?: boolean;
}

function initials(name: string): string {
  const words = name.replace(/["'.]/g, "").split(/\s+/).filter(Boolean);
  const first = words[0]?.[0] ?? "?";
  const last = words.length > 1 ? words[words.length - 1][0] : "";
  return `${first}${last}`.toUpperCase();
}

export function Avatar({ name, size = 40, ghost = false }: AvatarProps) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: radii.pill,
        backgroundColor: ghost ? "transparent" : palette.accentMuted,
        borderWidth: ghost ? 1 : 0,
        borderStyle: ghost ? "dashed" : "solid",
        borderColor: palette.border,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <AppText
        variant="caption"
        tone={ghost ? "tertiary" : "accent"}
        style={{ fontWeight: "600", fontSize: size * 0.35, lineHeight: size * 0.5 }}
      >
        {initials(name)}
      </AppText>
    </View>
  );
}
