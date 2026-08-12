/** Theme barrel: all design tokens plus useTheme(), which resolves the active palette from the system color scheme. */

import { useColorScheme, type ViewStyle } from "react-native";

import { dark, light, type Palette } from "./colors";

export { brand, dark, light } from "./colors";
export type { Palette } from "./colors";
export { spacing } from "./spacing";
export type { SpacingToken } from "./spacing";
export { typography } from "./typography";
export type { TypeStyle, TypographyVariant } from "./typography";
export { radii } from "./radii";
export type { RadiusToken } from "./radii";

/** Elevation presets per DESIGN.md (RN shadow + Android elevation). */
export const elevation = {
  none: {},
  low: {
    shadowColor: "#000000",
    shadowOpacity: 0.06,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 2 },
    elevation: 2,
  },
  medium: {
    shadowColor: "#000000",
    shadowOpacity: 0.1,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 },
    elevation: 6,
  },
} as const satisfies Record<string, ViewStyle>;

export type ElevationToken = keyof typeof elevation;

/** Returns the active color palette, following the system scheme (CONVENTIONS: system default). */
export function useTheme(): Palette {
  return useColorScheme() === "dark" ? dark : light;
}
