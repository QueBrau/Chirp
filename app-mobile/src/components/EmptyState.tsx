/**
 * EmptyState per DESIGN.md §5: emoji glyph (40), headline, one-line caption,
 * optional accent Button. Friendly — never a blank screen.
 */

import { Text, View } from "react-native";

import { metrics, spacing } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

export interface EmptyStateProps {
  title: string;
  message?: string;
  /** Friendly emoji glyph above the headline. */
  emoji?: string;
  /** Renders a primary Button when provided (with onAction). */
  actionLabel?: string;
  onAction?: () => void;
}

export function EmptyState({ title, message, emoji, actionLabel, onAction }: EmptyStateProps) {
  return (
    <View style={{ alignItems: "center", paddingVertical: spacing.xxl, gap: spacing.sm }}>
      {emoji !== undefined ? (
        <Text style={{ fontSize: metrics.emptyGlyph, lineHeight: metrics.emptyGlyph + spacing.sm }}>
          {emoji}
        </Text>
      ) : null}
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
