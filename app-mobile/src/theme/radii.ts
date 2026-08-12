/** Corner radius tokens per DESIGN.md §4: card 20, pill 999, input 14, avatar 16, thumbnail 12. */

export const radii = {
  card: 20,
  input: 14,
  /** Squircle-feel avatar corners (GradientAvatar/Avatar). */
  avatar: 16,
  thumb: 12,
  /** Floating tab bar container (§5). */
  tabBar: 28,
  pill: 999,

  // ---- Legacy v1 aliases (pre-v2 scale) — prefer the named tokens above ----
  sm: 6,
  md: 10,
  lg: 16,
} as const;

export type RadiusToken = keyof typeof radii;
