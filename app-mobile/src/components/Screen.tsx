/** Screen shell: safe-area, themed background, standard horizontal padding, optional scroll + title. */

import type { ReactNode } from "react";
import { ScrollView, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";

export interface ScreenProps {
  children: ReactNode;
  /** Large screen title (DESIGN.md `display`). */
  title?: string;
  /** Supporting line under the title. */
  subtitle?: string;
  /** Wrap content in a ScrollView (default true — most scaffold screens are lists). */
  scroll?: boolean;
}

export function Screen({ children, title, subtitle, scroll = true }: ScreenProps) {
  const palette = useTheme();

  const header =
    title !== undefined ? (
      <View style={{ marginBottom: spacing.lg, gap: spacing.xs }}>
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
            paddingHorizontal: spacing.lg,
            paddingTop: spacing.lg,
            paddingBottom: spacing.xxl,
          }}
        >
          {header}
          {children}
        </ScrollView>
      ) : (
        <View style={{ flex: 1, paddingHorizontal: spacing.lg, paddingTop: spacing.lg }}>
          {header}
          {children}
        </View>
      )}
    </SafeAreaView>
  );
}
