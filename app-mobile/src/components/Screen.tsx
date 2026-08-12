/**
 * Screen shell per DESIGN.md §5: safe-area, canvas bg, gutter padding, display
 * title + caption subtitle header (24 top pad, no nav chrome), and bottom
 * clearance so scroll content never hides under the floating tab bar.
 */

import type { ReactNode } from "react";
import { ScrollView, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { spacing, TAB_BAR_CLEARANCE, useTheme } from "@/theme";

import { AppText } from "./AppText";

export interface ScreenProps {
  children: ReactNode;
  /** Large screen title (type scale `display`). */
  title?: string;
  /** Caption subtitle under the title, in inkSecondary. */
  subtitle?: string;
  /** Wrap content in a ScrollView (default true — most screens are lists). */
  scroll?: boolean;
}

export function Screen({ children, title, subtitle, scroll = true }: ScreenProps) {
  const palette = useTheme();

  const header =
    title !== undefined ? (
      <View style={{ marginBottom: spacing.xl, gap: spacing.xs }}>
        <AppText variant="display">{title}</AppText>
        {subtitle !== undefined ? (
          <AppText variant="caption" tone="secondary">
            {subtitle}
          </AppText>
        ) : null}
      </View>
    ) : null;

  return (
    <SafeAreaView edges={["top"]} style={{ flex: 1, backgroundColor: palette.bg }}>
      {scroll ? (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={{
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.xl,
            paddingBottom: TAB_BAR_CLEARANCE,
          }}
          showsVerticalScrollIndicator={false}
        >
          {header}
          {children}
        </ScrollView>
      ) : (
        <View style={{ flex: 1, paddingHorizontal: spacing.gutter, paddingTop: spacing.xl }}>
          {header}
          {children}
        </View>
      )}
    </SafeAreaView>
  );
}
