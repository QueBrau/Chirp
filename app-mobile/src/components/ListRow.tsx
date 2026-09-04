/** List row primitive: left accessory + headline title / caption subtitle + right accessory, hairline divider. */

import type { ReactNode } from "react";
import { Pressable, StyleSheet, View } from "react-native";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";

export interface ListRowProps {
  title: string;
  /** String renders as caption text; a ReactNode renders as-is (e.g. icon + text). */
  subtitle?: string | ReactNode;
  left?: ReactNode;
  right?: ReactNode;
  onPress?: () => void;
  /**
   * What a screen reader announces for a tappable row. Defaults to `title` (c307).
   *
   * Only needed when the visible title is not the whole story - a row whose title is
   * "Appearance" and whose subtitle carries the current value, say. The default is
   * deliberate rather than a required prop: this component has ~25 call sites across 12
   * screens including Sign out, and a required label would have meant touching all of
   * them to fix an omission none of them caused.
   */
  accessibilityLabel?: string;
  /** Draw the bottom hairline (default true; disable on the last row of a group). */
  divider?: boolean;
}

export function ListRow({
  title,
  subtitle,
  left,
  right,
  onPress,
  accessibilityLabel,
  divider = true,
}: ListRowProps) {
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
        <AppText variant="headline" numberOfLines={1}>
          {title}
        </AppText>
        {typeof subtitle === "string" ? (
          <AppText variant="caption" tone="secondary" numberOfLines={2}>
            {subtitle}
          </AppText>
        ) : (
          subtitle ?? null
        )}
      </View>
      {right}
    </View>
  );

  if (onPress) {
    return (
      <Pressable
        // c307: a tappable row announced only its text content, with no indication it
        // could be activated at all - across ~25 call sites including Sign out and
        // Appearance. Role and name together, matching Fab.tsx's existing pattern: the
        // role alone is what VotePill had, and that still reads as an unlabeled button.
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel ?? title}
        onPress={onPress}
        style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
      >
        {content}
      </Pressable>
    );
  }
  // No onPress: this is static content, and giving it a button role would announce a
  // control that does nothing. Deliberately left bare.
  return content;
}
