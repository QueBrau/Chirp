/** Full type scale per DESIGN.md §3 (system font). Money/scores always tabular-nums via `stat`. */

import type { TextStyle } from "react-native";

export interface TypeStyle {
  fontSize: number;
  lineHeight: number;
  fontWeight: NonNullable<TextStyle["fontWeight"]>;
  letterSpacing?: number;
  textTransform?: TextStyle["textTransform"];
  fontVariant?: TextStyle["fontVariant"];
}

export const typography = {
  /** Screen titles ("Home"). */
  display: { fontSize: 30, lineHeight: 35, fontWeight: "800", letterSpacing: -0.5 },
  /** Card headlines, modal titles. */
  title: { fontSize: 20, lineHeight: 25, fontWeight: "700", letterSpacing: -0.3 },
  /** List row titles, post authors. */
  headline: { fontSize: 16, lineHeight: 21, fontWeight: "700" },
  /** Post bodies, descriptions. */
  body: { fontSize: 15, lineHeight: 21, fontWeight: "400" },
  /** Emphasized body. */
  bodyBold: { fontSize: 15, lineHeight: 21, fontWeight: "600" },
  /** Timestamps, metadata. */
  caption: { fontSize: 12.5, lineHeight: 17, fontWeight: "500" },
  /** Chip labels, section eyebrows — uppercase. */
  micro: {
    fontSize: 11,
    lineHeight: 14,
    fontWeight: "600",
    letterSpacing: 0.4,
    textTransform: "uppercase",
  },
  /** Scores, money amounts — tabular digits. */
  stat: { fontSize: 17, lineHeight: 22, fontWeight: "800", fontVariant: ["tabular-nums"] },
} satisfies Record<string, TypeStyle>;

export type TypographyVariant = keyof typeof typography;
