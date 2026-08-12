/** 4-based spacing scale per DESIGN.md §4: 4 / 8 / 12 / 16 / 20 / 24 / 32. Screen gutter = 20. */

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  /** Standard screen horizontal gutter (§4). */
  gutter: 20,
  xl: 24,
  xxl: 32,
  /** Legacy oversize step (pre-v2) — prefer xl/xxl in new code. */
  xxxl: 48,
} as const;

export type SpacingToken = keyof typeof spacing;
