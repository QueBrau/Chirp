/**
 * Avatar (compat wrapper): regular members render as GradientAvatar (DESIGN.md §5);
 * ghost members (users.is_ghost) keep a dashed muted squircle.
 */

import { View } from "react-native";

import { initials } from "@/lib/initials";
import { radii, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { GradientAvatar } from "./GradientAvatar";

export interface AvatarProps {
  name: string;
  /** Side length; canonical sizes 32/40/48. Default 40. */
  size?: number;
  /** Placeholder historical member (users.is_ghost). */
  ghost?: boolean;
}

export function Avatar({ name, size = 40, ghost = false }: AvatarProps) {
  const palette = useTheme();

  if (!ghost) {
    return <GradientAvatar name={name} size={size} />;
  }

  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: radii.avatar,
        backgroundColor: "transparent",
        borderWidth: 1,
        borderStyle: "dashed",
        borderColor: palette.inkFaint,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <AppText
        tone="faint"
        style={{ fontWeight: "600", fontSize: size * 0.35, lineHeight: size * 0.45 }}
      >
        {initials(name)}
      </AppText>
    </View>
  );
}
