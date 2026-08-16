/**
 * Shared scroll-direction state for the floating tab bar's auto-hide (user
 * ask: "be a dynamic island that goes away when you scroll" — built as an
 * addition to the existing DESIGN.md §5 pill, not a redesign of it).
 *
 * `Screen` (src/components/Screen.tsx) and `FloatingTabBar`
 * (app/(tabs)/_layout.tsx) are far apart in the component tree, so this
 * context is the wire between them: Screen's ScrollView writes scroll
 * direction into `visible` (a Reanimated shared value — 1 = shown, 0 =
 * hidden) as the user scrolls; FloatingTabBar reads it via useAnimatedStyle
 * to translate/fade itself off the bottom edge.
 *
 * Mounted once, above <Tabs> (app/(tabs)/_layout.tsx), so every tab's own
 * stack — and every screen pushed within it — shares one visibility value.
 * Screens rendered OUTSIDE that subtree (the (auth) stack, which also uses
 * <Screen>) have no provider — useTabBarVisibility() returns null there and
 * callers must treat that as "nothing to wire up," not an error.
 */

import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import { AccessibilityInfo, Platform } from "react-native";
import { useSharedValue, withTiming, type SharedValue } from "react-native-reanimated";

/** Fade/slide duration for both hide and reveal — fast enough to feel like a
 * direct response to the scroll gesture, slow enough not to feel like a snap. */
const VISIBILITY_ANIM_MS = 220;

/** Cumulative downward scroll (px) required before the bar hides — small
 * enough to feel responsive, large enough that a few pixels of rubber-band
 * jitter at rest never flickers it. Any upward movement resets this and
 * reveals immediately (no threshold on the way back). */
export const SCROLL_HIDE_THRESHOLD = 24;

/** Below this scroll offset the bar is always shown, full stop — matches
 * "always visible at/near the top of the scroll." */
export const SCROLL_TOP_THRESHOLD = 12;

/**
 * react-native-web's Reanimated support is real but imperfect, and this
 * build's auth gate means the interactive hide/reveal loop can't be proven
 * reliable in a browser (see app-mobile task notes). Per the "degrade to
 * always-visible rather than ship a bar that can vanish permanently" rule,
 * the HIDE side of the gesture is a native-only enhancement — reveal (top of
 * scroll / scroll up / screen focus) still runs everywhere, so web always
 * converges on a visible bar instead of a possibly-stuck-hidden one.
 */
export const AUTO_HIDE_SUPPORTED = Platform.OS !== "web";

export interface TabBarVisibilityContextValue {
  /** 1 = fully shown, 0 = fully hidden. FloatingTabBar derives its
   * translateY/opacity from this; Screen's scroll handler writes to it. */
  visible: SharedValue<number>;
  /** Mirrors AccessibilityInfo's Reduce Motion setting, kept live via its
   * change event. animateTabBarVisibility snaps instead of tweening when set. */
  reducedMotion: SharedValue<boolean>;
  /** Force the bar back to fully visible, honoring reduced motion. Screen
   * calls this on mount/focus so nobody lands on a screen with the bar
   * hidden and no scroll gesture yet available to bring it back. */
  reveal: () => void;
}

const TabBarVisibilityContext = createContext<TabBarVisibilityContextValue | null>(null);

/**
 * Animates (or snaps, under reduced motion) `visible` toward `target`. Marked
 * "worklet" so the identical implementation runs on the UI thread when called
 * from inside Screen's scroll handler (already a worklet) and on the JS
 * thread when called from a plain event handler like `reveal()` — Reanimated
 * keeps both call sites consistent through the shared value itself.
 */
export function animateTabBarVisibility(
  visible: SharedValue<number>,
  reducedMotion: SharedValue<boolean>,
  target: 0 | 1,
) {
  "worklet";
  visible.value = reducedMotion.value ? target : withTiming(target, { duration: VISIBILITY_ANIM_MS });
}

export function TabBarVisibilityProvider({ children }: { children: ReactNode }) {
  const visible = useSharedValue(1);
  const reducedMotion = useSharedValue(false);

  useEffect(() => {
    let cancelled = false;
    AccessibilityInfo.isReduceMotionEnabled()
      .then((enabled) => {
        if (!cancelled) reducedMotion.value = enabled;
      })
      .catch(() => {
        // Fail soft: worst case, motion just isn't reduced.
      });
    const subscription = AccessibilityInfo.addEventListener("reduceMotionChanged", (enabled) => {
      reducedMotion.value = enabled;
    });
    return () => {
      cancelled = true;
      subscription.remove();
    };
  }, [reducedMotion]);

  const value = useMemo<TabBarVisibilityContextValue>(
    () => ({
      visible,
      reducedMotion,
      reveal: () => animateTabBarVisibility(visible, reducedMotion, 1),
    }),
    [visible, reducedMotion],
  );

  return <TabBarVisibilityContext.Provider value={value}>{children}</TabBarVisibilityContext.Provider>;
}

/** Null outside the provider (e.g. the (auth) stack) — callers must handle that. */
export function useTabBarVisibility(): TabBarVisibilityContextValue | null {
  return useContext(TabBarVisibilityContext);
}
