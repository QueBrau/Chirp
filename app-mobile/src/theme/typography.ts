/** Type scale (display/title/body/caption) per DESIGN.md; system font. */

import type { TextStyle } from "react-native";

export interface TypeStyle {
  fontSize: number;
  lineHeight: number;
  fontWeight: NonNullable<TextStyle["fontWeight"]>;
}

export const typography = {
  display: { fontSize: 28, lineHeight: 34, fontWeight: "700" },
  title: { fontSize: 20, lineHeight: 26, fontWeight: "600" },
  body: { fontSize: 16, lineHeight: 22, fontWeight: "400" },
  caption: { fontSize: 13, lineHeight: 18, fontWeight: "400" },
} as const satisfies Record<string, TypeStyle>;

export type TypographyVariant = keyof typeof typography;
