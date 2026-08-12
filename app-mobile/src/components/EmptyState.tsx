/** Centered empty/placeholder state for lists and stub sections. */

import { View } from "react-native";

import { spacing } from "@/theme";

import { AppText } from "./AppText";

export interface EmptyStateProps {
  title: string;
  message?: string;
}

export function EmptyState({ title, message }: EmptyStateProps) {
  return (
    <View style={{ alignItems: "center", paddingVertical: spacing.xxxl, gap: spacing.sm }}>
      <AppText variant="title" tone="secondary" style={{ textAlign: "center" }}>
        {title}
      </AppText>
      {message !== undefined ? (
        <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
          {message}
        </AppText>
      ) : null}
    </View>
  );
}
