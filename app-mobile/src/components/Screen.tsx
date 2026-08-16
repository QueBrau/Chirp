/**
 * Screen shell per DESIGN.md §5: safe-area, canvas bg, gutter padding, display
 * title + caption subtitle header (24 top pad, no nav chrome), and bottom
 * clearance so scroll content never hides under the floating tab bar.
 *
 * §10.1 header zones (Home/Yak/Orgs): optional `eyebrow` (micro uppercase line
 * above the title) and `accentBarColor` (the 4x28 accent bar under the title —
 * pass the screen's own "one gold moment" color; omit on screens that don't
 * use the header-zone pattern, e.g. Messages/Profile, and nothing renders).
 *
 * BACK CONTROL: every layout in this app sets `headerShown: false`, so pushed
 * screens had no visible way back — only two of them hand-rolled their own
 * button. That is worst on web, where react-native-web has no edge-swipe
 * gesture at all, making those screens genuine dead ends. The control lives
 * here rather than per-screen so the next pushed screen gets it for free, and
 * it defaults to `router.canGoBack()`: tab roots show nothing, pushed screens
 * show it automatically.
 *
 * TAB BAR AUTO-HIDE: this owns the ScrollView, so it is also what drives the
 * floating tab bar out of the way on scroll (see src/nav/TabBarVisibility).
 */

import { useRouter } from "expo-router";
import { Feather } from "@expo/vector-icons";
import { useEffect, type ReactNode } from "react";
import { Pressable, ScrollView, View } from "react-native";
import Animated, { useAnimatedScrollHandler, useSharedValue } from "react-native-reanimated";
import { SafeAreaView } from "react-native-safe-area-context";

import {
  animateTabBarVisibility,
  AUTO_HIDE_SUPPORTED,
  SCROLL_HIDE_THRESHOLD,
  SCROLL_TOP_THRESHOLD,
  useTabBarVisibility,
} from "@/nav/TabBarVisibility";
import { metrics, radii, spacing, TAB_BAR_CLEARANCE, useTheme } from "@/theme";

import { AppText } from "./AppText";

const AnimatedScrollView = Animated.createAnimatedComponent(ScrollView);

export interface ScreenProps {
  children: ReactNode;
  /** Large screen title (type scale `display`). */
  title?: string;
  /** Caption subtitle under the title, in inkSecondary. */
  subtitle?: string;
  /** Micro uppercase eyebrow above the title (§10.1 — e.g. "UNC GREENSBORO"). */
  eyebrow?: string;
  /** Renders the §10.1/§10.4 accent bar (4x28, radius 2) under the title in this color. */
  accentBarColor?: string;
  /** Canvas override (Yak's deep-navy campus wash, §10.5) — default `palette.bg`. */
  backgroundColor?: string;
  /** Wrap content in a ScrollView (default true — most screens are lists). */
  scroll?: boolean;
  /**
   * Show the back control. Defaults to `router.canGoBack()`, which is the right
   * answer almost everywhere: false on tab roots, true on pushed screens. Pass
   * `false` to suppress it, or `true` to force it alongside a custom `onBack`.
   */
  showBack?: boolean;
  /** Custom back behaviour; defaults to `router.back()`. */
  onBack?: () => void;
  /**
   * Icon color for the back control. Defaults to the theme's ink. Screens that
   * override `backgroundColor` with a dark canvas (Yak's navy wash) must pass a
   * light tint here or the chevron disappears into the background.
   */
  backTint?: string;
}

export function Screen({
  children,
  title,
  subtitle,
  eyebrow,
  accentBarColor,
  backgroundColor,
  scroll = true,
  showBack,
  onBack,
  backTint,
}: ScreenProps) {
  const palette = useTheme();
  const router = useRouter();
  const tabBar = useTabBarVisibility();

  // Hooks can't be conditional, but the provider is absent outside the tabs
  // subtree (the (auth) stack also renders <Screen>). Local fallbacks keep the
  // scroll handler valid there; writing to them simply affects nothing.
  const fallbackVisible = useSharedValue(1);
  const fallbackReducedMotion = useSharedValue(false);
  const visible = tabBar?.visible ?? fallbackVisible;
  const reducedMotion = tabBar?.reducedMotion ?? fallbackReducedMotion;

  const lastOffset = useSharedValue(0);
  const downwardTravel = useSharedValue(0);

  // Reveal whenever a screen mounts. Without this a user can navigate while the
  // bar is hidden and land somewhere short enough to have no scroll gesture
  // available to bring it back — stranded with no navigation.
  useEffect(() => {
    tabBar?.reveal();
  }, [tabBar]);

  const scrollHandler = useAnimatedScrollHandler({
    onScroll: (event) => {
      const y = event.contentOffset.y;
      const delta = y - lastOffset.value;
      lastOffset.value = y;

      if (y <= SCROLL_TOP_THRESHOLD) {
        // At rest near the top the bar is always shown, full stop.
        downwardTravel.value = 0;
        animateTabBarVisibility(visible, reducedMotion, 1);
        return;
      }
      if (delta > 0) {
        // Accumulate downward travel so a few pixels of rubber-band jitter
        // never flickers the bar; only a deliberate scroll hides it.
        downwardTravel.value += delta;
        if (AUTO_HIDE_SUPPORTED && downwardTravel.value > SCROLL_HIDE_THRESHOLD) {
          animateTabBarVisibility(visible, reducedMotion, 0);
        }
      } else if (delta < 0) {
        // Any upward movement reveals immediately — no threshold on the way back.
        downwardTravel.value = 0;
        animateTabBarVisibility(visible, reducedMotion, 1);
      }
    },
  });

  const backVisible = showBack ?? router.canGoBack();

  const backControl = backVisible ? (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="Go back"
      hitSlop={spacing.md}
      onPress={onBack ?? (() => router.back())}
      style={({ pressed }) => ({
        width: 36,
        height: 36,
        borderRadius: radii.pill,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: palette.surfaceAlt,
        marginBottom: spacing.md,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather name="chevron-left" size={20} color={backTint ?? palette.ink} />
    </Pressable>
  ) : null;

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
        <AnimatedScrollView
          style={{ flex: 1 }}
          contentContainerStyle={{
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.xl,
            paddingBottom: TAB_BAR_CLEARANCE,
          }}
          showsVerticalScrollIndicator={false}
          onScroll={scrollHandler}
          scrollEventThrottle={16}
        >
          {backControl}
          {header}
          {children}
        </AnimatedScrollView>
      ) : (
        // Non-scrolling screens have no gesture to reveal the bar with, so they
        // never drive the auto-hide — the bar stays as the last screen left it,
        // which the mount-time reveal above guarantees is "visible".
        <View style={{ flex: 1, paddingHorizontal: spacing.gutter, paddingTop: spacing.xl }}>
          {backControl}
          {header}
          {children}
        </View>
      )}
    </SafeAreaView>
  );
}
