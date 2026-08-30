/** Badge (compat wrapper over Chip): soft pill with micro label per DESIGN.md §5. */

import { Chip } from "./Chip";

/**
 * Same five names as ChipVariant, spelled out rather than aliased so Badge's
 * public surface stays its own. They are passed straight through: a tone that
 * ever stops being a Chip variant fails to compile at the <Chip> below, which
 * is the check the old identity-map object was doing by hand (c239).
 */
export type BadgeTone = "accent" | "success" | "danger" | "warning" | "neutral";

export interface BadgeProps {
  label: string;
  tone?: BadgeTone;
}

export function Badge({ label, tone = "neutral" }: BadgeProps) {
  return <Chip label={label} variant={tone} />;
}
