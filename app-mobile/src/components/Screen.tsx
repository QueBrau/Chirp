/**
 * Screen shell per DESIGN.md §5: safe-area, canvas bg, gutter padding, display
 * title + caption subtitle header (24 top pad, no nav chrome), and bottom
 * clearance so scroll content never hides under the floating tab bar.
 *
 * §10.1 header zones (Home/Yak/Orgs): optional `eyebrow` (micro uppercase line
 * above the title) and `accentBarColor` (the 4x28 accent bar under the title —
 * pass the screen's own "one gold moment" color; omit on screens that don't
 * use the header-zone pattern, e.g. Messages/Profile, and nothing renders).
 */

import type { ReactNode } from "react";
import { ScrollView, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { metrics, spacing, TAB_BAR_CLEARANCE, useTheme } from "@/theme";

import { AppText } from "./AppText";

export interface ScreenProps {
  children: ReactNode;
  /** Large screen title (type scale `display`). */
  title?: string;
  /** Caption subtitle under the title, in inkSecondary. */
  subtitle?: string;
  /** Micro uppercase eyebrow above the title (§10.1 — e.g. "UNC GREENSBORO · SPARTANS"). */
  eyebrow?: string;
  /** Renders the §10.1/§10.4 accent bar (4x28, radius 2) under the title in this color. */
  accentBarColor?: string;
  /** Canvas override (Yak's deep-navy campus wash, §10.5) — default `palette.bg`. */
  backgroundColor?: string;
  /** Wrap content in a ScrollView (default true — most screens are lists). */
  scroll?: boolean;
}

export function Screen({
  children,
  title,
  subtitle,
  eyebrow,
  accentBarColor,
  backgroundColor,
  scroll = true,
}: ScreenProps) {
  const palette = useTheme();

  const header =
    title !== undefined ? (
      <View style={{ marginBottom: spacing.xl, gap: spacing.xs }}>
        {eyebrow !== undefined ? (
          <AppText variant="micro" tone="secondary">
            {eyebrow}
          </AppText>
        ) : null}
        <AppText variant="display">{title}</AppText>
        {accentBarColor !== undefined ? (
          <View
            style={{
              width: metrics.accentBarWidth,
              height: metrics.accentBarHeight,
              borderRadius: metrics.accentBarRadius,
              backgroundColor: accentBarColor,
              marginTop: spacing.xs,
            }}
          />
        ) : null}
        {subtitle !== undefined ? (
          <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.xs }}>
            {subtitle}
          </AppText>
        ) : null}
      </View>
    ) : null;

  return (
    <SafeAreaView edges={["top"]} style={{ flex: 1, backgroundColor: backgroundColor ?? palette.bg }}>
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
