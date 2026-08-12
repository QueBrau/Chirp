/** List row primitive: left accessory + title/subtitle + right accessory, hairline divider. */

import type { ReactNode } from "react";
import { Pressable, StyleSheet, View } from "react-native";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";

export interface ListRowProps {
  title: string;
  subtitle?: string;
  left?: ReactNode;
  right?: ReactNode;
  onPress?: () => void;
  /** Draw the bottom hairline (default true; disable on the last row of a group). */
  divider?: boolean;
}

export function ListRow({ title, subtitle, left, right, onPress, divider = true }: ListRowProps) {
  const palette = useTheme();

  const content = (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.md,
        paddingVertical: spacing.md,
        borderBottomWidth: divider ? StyleSheet.hairlineWidth : 0,
        borderBottomColor: palette.border,
      }}
    >
      {left}
      <View style={{ flex: 1, gap: spacing.xs }}>
        <AppText numberOfLines={1}>{title}</AppText>
        {subtitle !== undefined ? (
          <AppText variant="caption" tone="secondary" numberOfLines={2}>
            {subtitle}
          </AppText>
        ) : null}
      </View>
      {right}
    </View>
  );

  if (onPress) {
    return (
      <Pressable onPress={onPress} style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}>
        {content}
      </Pressable>
    );
  }
  return content;
}
