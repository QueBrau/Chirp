/**
 * Running-balance trend (DESIGN §11).
 *
 * ONE series, so it is one colour and carries no legend — the card's title says
 * what is plotted, and a legend box with a single swatch would just restate it.
 * The colour is the live `accent`, which means it wears the org's colour inside
 * OrgAccentScope (DESIGN §8.6) without this component knowing anything about orgs.
 */

import { useState } from "react";
import { View, type LayoutChangeEvent } from "react-native";
import Svg, { Circle, Line, Path } from "react-native-svg";

import { AppText } from "@/components/AppText";
import { spacing, typography, useTheme } from "@/theme";

import { trendGeometry, type Point } from "./geometry";

export interface BalanceTrendProps {
  points: Point[];
  /** Formats a y value for the range caption. Kept out so cents never leak in here. */
  format: (value: number) => string;
  height?: number;
}

/** Radius of the end dot. 4.5 clears the spec's 8px minimum mark size. */
const DOT_RADIUS = 4.5;
/** Inset so the dot and its surface ring are never clipped by the viewBox edge. */
const INSET = DOT_RADIUS + 2;

export function BalanceTrend({ points, format, height = 120 }: BalanceTrendProps) {
  const palette = useTheme();
  // Measured rather than assumed: the card's width depends on the screen gutter and
  // a hardcoded width would overflow on small phones and stripe on large ones.
  const [width, setWidth] = useState(0);
  const onLayout = (event: LayoutChangeEvent) => setWidth(event.nativeEvent.layout.width);

  const geometry = width > 0 ? trendGeometry(points, { width, height, inset: INSET }) : null;

  return (
    <View onLayout={onLayout}>
      <View style={{ height }}>
        {geometry !== null && geometry.line !== "" ? (
          <Svg width={width} height={height}>
            {/* Zero line only when the balance actually crosses it. Drawing it always
                would put a hairline at the bottom of every healthy chapter's chart
                and read as an axis that means nothing. */}
            {geometry.zeroY !== null ? (
              <Line
                x1={0}
                y1={geometry.zeroY}
                x2={width}
                y2={geometry.zeroY}
                stroke={palette.border}
                strokeWidth={1}
              />
            ) : null}
            <Path d={geometry.area} fill={palette.accent} fillOpacity={0.1} />
            <Path
              d={geometry.line}
              fill="none"
              stroke={palette.accent}
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
            {geometry.last !== null ? (
              <Circle
                cx={geometry.last.x}
                cy={geometry.last.y}
                r={DOT_RADIUS}
                fill={palette.accent}
                // Surface ring, not a border: it keeps the dot legible where it sits
                // on top of the line it terminates.
                stroke={palette.surface}
                strokeWidth={2}
              />
            ) : null}
          </Svg>
        ) : null}
      </View>

      {/*
        ONE caption carrying both ends of the RANGE, rather than a number in each
        bottom corner. Corner-anchored numbers read as "started here, ended there"
        on a time axis — the first draft of this chart said $960 on the left and
        $2,400 on the right while the balance actually ended at $1,995.
      */}
      {geometry !== null && geometry.line !== "" ? (
        <AppText
          variant="caption"
          tone="tertiary"
          style={{
            marginTop: spacing.sm,
            fontVariant: typography.stat.fontVariant,
          }}
        >
          {`Low ${format(geometry.min)} · High ${format(geometry.max)}`}
        </AppText>
      ) : null}
    </View>
  );
}
