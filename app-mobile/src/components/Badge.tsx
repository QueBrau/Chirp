/** Badge (compat wrapper over Chip): soft pill with micro label per DESIGN.md §5. */

import { Chip, type ChipVariant } from "./Chip";

export type BadgeTone = "accent" | "success" | "danger" | "warning" | "neutral";

export interface BadgeProps {
  label: string;
  tone?: BadgeTone;
}

const TONE_TO_VARIANT: Record<BadgeTone, ChipVariant> = {
  accent: "accent",
  success: "success",
  danger: "danger",
  warning: "warning",
  neutral: "neutral",
};

export function Badge({ label, tone = "neutral" }: BadgeProps) {
  return <Chip label={label} variant={TONE_TO_VARIANT[tone]} />;
}
