/**
 * A single ratio against a target (DESIGN §11).
 *
 * THIS IS NOT A TWO-SLICE PIE, deliberately. "Collected vs outstanding" is one
 * number against a limit, and a meter answers it in a glance where a two-slice pie
 * makes the reader compare two angles to learn one percentage.
 *
 * Built from Views rather than SVG: it is two rounded rectangles, and reaching for
 * a drawing surface to render a rectangle costs a native view for nothing.
 */

import { View } from "react-native";

import { AppText } from "@/components/AppText";
import { radii, spacing, useTheme } from "@/theme";

export interface ProgressMeterProps {
  /** 0..1. Callers clamp; this clamps again because a meter cannot overfill. */
  fraction: number;
  /** Accessible description of what is being measured. */
  label: string;
}

const TRACK_HEIGHT = 12;

export function ProgressMeter({ fraction, label }: ProgressMeterProps) {
  const palette = useTheme();
  const pct = Math.max(0, Math.min(fraction, 1));

  return (
    <View
      accessibilityRole="progressbar"
      accessibilityLabel={label}
      accessibilityValue={{ min: 0, max: 100, now: Math.round(pct * 100) }}
      style={{
        height: TRACK_HEIGHT,
        borderRadius: radii.pill,
        // The unfilled track is a lighter step of the FILL's own ramp, not a neutral
        // grey: state then reads across the whole bar instead of only the filled part.
        backgroundColor: palette.accentSoft,
        overflow: "hidden",
        marginBottom: spacing.sm,
      }}
    >
      <View
        style={{
          height: "100%",
          width: `${pct * 100}%`,
          borderRadius: radii.pill,
          backgroundColor: palette.accent,
        }}
      />
    </View>
  );
}
