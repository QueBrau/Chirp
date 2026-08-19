/**
 * Spend-by-category donut (DESIGN §11).
 *
 * A donut is legal here for the one job it does well — part-to-whole AT A GLANCE,
 * with few segments. It is not legal for comparing close values, which is why every
 * slice's exact amount sits in the legend beside it: nobody should ever have to
 * judge two similar angles. The slice count is capped upstream in
 * `spendByCategory`, because past roughly six segments adjacent slices stop being
 * separable at any palette.
 */

import { View } from "react-native";
import Svg, { Path } from "react-native-svg";

import { AppText } from "@/components/AppText";
import type { CategorySlice } from "@/lib/treasury";
import { assignCategorySlots } from "@/lib/treasury";
import { radii, spacing, typography, useTheme } from "@/theme";

import { donutSegments } from "./geometry";

export interface CategoryDonutProps {
  slices: CategorySlice[];
  format: (cents: number) => string;
  /** Centre caption under the total, e.g. "total out". */
  centerCaption: string;
  size?: number;
  thickness?: number;
}

export function CategoryDonut({
  slices,
  format,
  centerCaption,
  size = 168,
  thickness = 36,
}: CategoryDonutProps) {
  const palette = useTheme();

  const segments = donutSegments(
    slices.map((slice) => slice.cents),
    { size, thickness, gapPx: 2 },
  );
  const total = slices.reduce((sum, slice) => sum + slice.cents, 0);

  // Slots are assigned from the label set, so a category keeps its colour when the
  // amounts move. "Other" is excluded — it wears the neutral, never a hue.
  const slots = assignCategorySlots(
    slices.filter((slice) => !slice.isOther).map((slice) => slice.label),
    palette.chartCategorical.length,
  );
  const colorFor = (slice: CategorySlice): string =>
    slice.isOther
      ? palette.chartOther
      : palette.chartCategorical[slots.get(slice.label) ?? 0];

  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
      <View style={{ width: size, height: size }}>
        <Svg width={size} height={size}>
          {segments.map((segment, index) => (
            <Path
              key={slices[index]?.label ?? index}
              d={segment.d}
              fill={colorFor(slices[index])}
              // evenodd matters only for the single-category full ring, where the
              // inner circle has to punch the hole rather than fill it.
              fillRule="evenodd"
            />
          ))}
        </Svg>

        {/* The total lives in the hole rather than in another caption: it is the
            one number the ring is a breakdown OF, so it belongs inside it. */}
        <View
          pointerEvents="none"
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <AppText variant="stat" style={{ fontVariant: typography.stat.fontVariant }}>
            {format(total)}
          </AppText>
          <AppText variant="caption" tone="tertiary">
            {centerCaption}
          </AppText>
        </View>
      </View>

      {/*
        The legend is not optional decoration. With more than one series it is the
        dependable identity channel — colour alone is never the only way to tell the
        slices apart — and it is what makes the amounts readable without comparing
        angles. Labels wear TEXT tokens; only the swatch carries the series colour.
      */}
      <View style={{ flex: 1, gap: spacing.sm }}>
        {slices.map((slice) => (
          <View
            key={slice.label}
            style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}
          >
            <View
              style={{
                width: 10,
                height: 10,
                borderRadius: radii.sm / 2,
                backgroundColor: colorFor(slice),
              }}
            />
            <AppText
              variant="caption"
              tone="secondary"
              numberOfLines={1}
              style={{ flex: 1, textTransform: "capitalize" }}
            >
              {slice.label}
            </AppText>
            <AppText
              variant="bodyBold"
              style={{ fontVariant: typography.stat.fontVariant }}
            >
              {format(slice.cents)}
            </AppText>
          </View>
        ))}
      </View>
    </View>
  );
}
