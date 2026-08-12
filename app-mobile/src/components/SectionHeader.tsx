/**
 * SectionHeader: micro uppercase eyebrow (inkSecondary) with optional caption
 * note and right accessory. 8pt gap to the content below (DESIGN.md §4 rhythm).
 */

import type { ReactNode } from "react";
import { View, type ViewStyle } from "react-native";

import { spacing } from "@/theme";

import { AppText } from "./AppText";

export interface SectionHeaderProps {
  title: string;
  /** Small supporting note (e.g. "Append-only"). */
  caption?: string;
  /** Right-aligned accessory (e.g. a "See all" link). */
  right?: ReactNode;
  style?: ViewStyle;
}

export function SectionHeader({ title, caption, right, style }: SectionHeaderProps) {
  return (
    <View
      style={[
        {
          flexDirection: "row",
          alignItems: "flex-end",
          justifyContent: "space-between",
          gap: spacing.sm,
          marginBottom: spacing.sm,
        },
        style,
      ]}
    >
      <View style={{ flex: 1, gap: spacing.xs }}>
        <AppText variant="micro" tone="secondary">
          {title}
        </AppText>
        {caption !== undefined ? (
          <AppText variant="caption" tone="tertiary">
            {caption}
          </AppText>
        ) : null}
      </View>
      {right}
    </View>
  );
}
