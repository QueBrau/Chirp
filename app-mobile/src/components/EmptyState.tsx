/**
 * EmptyState per DESIGN.md §5: geometric mark (outlined circle drawn with Views —
 * never an emoji), headline, one-line caption, optional accent Button.
 * Friendly — never a blank screen.
 */

import { View } from "react-native";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

export interface EmptyStateProps {
  title: string;
  message?: string;
  /** Renders a primary Button when provided (with onAction). */
  actionLabel?: string;
  onAction?: () => void;
}

/** Outlined circle + accent dot: the standard empty-state mark. */
function GeometricMark() {
  const palette = useTheme();
  return (
    <View
      style={{
        width: 48,
        height: 48,
        borderRadius: 24,
        borderWidth: 2,
        borderColor: palette.accentSoft,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <View
        style={{
          width: 12,
          height: 12,
          borderRadius: 6,
          backgroundColor: palette.accent,
        }}
      />
    </View>
  );
}

export function EmptyState({ title, message, actionLabel, onAction }: EmptyStateProps) {
  return (
    <View style={{ alignItems: "center", paddingVertical: spacing.xxl, gap: spacing.sm }}>
      <GeometricMark />
      <AppText variant="headline" style={{ textAlign: "center" }}>
        {title}
      </AppText>
      {message !== undefined ? (
        <AppText variant="caption" tone="secondary" style={{ textAlign: "center" }} numberOfLines={2}>
          {message}
        </AppText>
      ) : null}
      {actionLabel !== undefined ? (
        <Button label={actionLabel} onPress={onAction} style={{ marginTop: spacing.sm }} />
      ) : null}
    </View>
  );
}
