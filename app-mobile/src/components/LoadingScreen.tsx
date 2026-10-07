/**
 * LoadingScreen (board c434, braul Oct 6, picked from four mockups).
 *
 * This is the IN-APP loading screen, shown while a returning user's session restores
 * (the `status === "loading"` window in app/(tabs)/_layout.tsx and join-chapter.tsx,
 * which used to render nothing). It is NOT the native launch screen: one build serves
 * every campus, and the native splash renders before any JS runs, so it cannot know
 * which campus to show. The chain on a cold start is native splash (Expo default today)
 * -> this screen -> the app.
 *
 * WHAT IT SHOWS. For a returning user: the campus PRIMARY filling the screen edge to
 * edge, the chirp mark in the campus SECONDARY, and the campus name. The campus comes
 * from device memory (auth/lastCampus.ts), because nothing about the campus is known on
 * a cold start until /auth/me and the campus lookup return. For a first launch or a
 * signed-out launch there is no memory, so it shows the neutral chirp tile on the app
 * canvas instead of guessing a school.
 *
 * No animation on the mark: the screen is usually up for a fraction of a second, and a
 * logo that starts moving just as it disappears reads as a glitch. A spinner appears
 * only if the load turns out slow.
 */

import { StatusBar } from "expo-status-bar";
import { useEffect, useState } from "react";
import { ActivityIndicator, View } from "react-native";

import { useLastCampus, type LastCampus } from "@/auth";
import { contrastRatio, light, spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { ChirpMark } from "./ChirpMark";

/** A load shorter than this never shows the spinner; it would only flash. */
const SPINNER_DELAY_MS = 800;
/** ActivityIndicator's default ("small") footprint; the slot below reserves exactly this. */
const SPINNER_SLOT = 20;

const WHITE = "#FFFFFF";

/**
 * The color for the mark and the campus name on `primary`.
 *
 * The campus secondary when it clears 4.5:1 against the primary. 4.5 rather than 3 on
 * purpose: the mark alone would only need WCAG 1.4.11's 3:1 for graphics, but this one
 * value also colors the campus NAME, which is text, so it has to clear the text bar. That
 * way a single color covers both. Navy and gold (UNCG) passes comfortably.
 *
 * Otherwise a campus whose two colors sit close together would render an invisible mark,
 * so it falls back to whichever of white and the light palette's ink (#101223, the
 * darkest text color the app already owns, not a new one) has the higher contrast
 * against the primary.
 */
export function campusForeground(primary: string, secondary: string): string {
  const MIN_TEXT_CONTRAST = 4.5;
  if (contrastRatio(secondary, primary) >= MIN_TEXT_CONTRAST) return secondary;
  return contrastRatio(WHITE, primary) >= contrastRatio(light.ink, primary) ? WHITE : light.ink;
}

/**
 * Status bar icon style over the campus primary: "light" (white icons) exactly when
 * white reads better on the primary than the light palette's ink does, the same
 * comparison campusForeground's fallback makes.
 *
 * NOT A FIXED LUMINANCE CUT. A cut like `relativeLuminance(primary) < 0.5` picks light
 * icons on every mid-tone primary (luminance roughly 0.18 to 0.5) even though dark icons
 * read better there; white and ink contrast equally near 0.18. Measuring contrast the
 * way the foreground rule does puts the icons on the same side of that line as the
 * white-or-ink fallback is.
 */
function campusStatusBarStyle(primary: string): "light" | "dark" {
  return contrastRatio(WHITE, primary) >= contrastRatio(light.ink, primary) ? "light" : "dark";
}

/** True once `delayMs` has passed since mount. Cleared on unmount. */
function useElapsed(delayMs: number): boolean {
  const [elapsed, setElapsed] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setElapsed(true), delayMs);
    return () => clearTimeout(timer);
  }, [delayMs]);
  return elapsed;
}

export interface LoadingScreenViewProps {
  /** The remembered campus, or null for the neutral first-launch / signed-out screen. */
  campus: LastCampus | null;
}

/** Presentational half: everything the screen draws, given the campus (or none). */
export function LoadingScreenView({ campus }: LoadingScreenViewProps) {
  const palette = useTheme();
  const slow = useElapsed(SPINNER_DELAY_MS);

  const fg = campus ? campusForeground(campus.primary, campus.secondary) : palette.inkSecondary;

  return (
    <View
      accessible
      accessibilityRole="progressbar"
      accessibilityLabel={campus ? `Loading Chirp for ${campus.name}` : "Loading Chirp"}
      style={{
        flex: 1,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: campus ? campus.primary : palette.bg,
      }}
    >
      {campus ? (
        // Light icons on a dark primary, dark icons on a light one.
        <StatusBar style={campusStatusBarStyle(campus.primary)} />
      ) : null}
      <View style={{ alignItems: "center", gap: spacing.md }}>
        {campus ? (
          <>
            <ChirpMark size={88} color={fg} eyeColor={campus.primary} />
            <AppText
              variant="headline"
              numberOfLines={2}
              style={{ color: fg, textAlign: "center", paddingHorizontal: spacing.xxl }}
            >
              {campus.name}
            </AppText>
          </>
        ) : (
          <ChirpMark
            size={72}
            tileColor={palette.accent}
            color={palette.onAccent}
            eyeColor={palette.accent}
          />
        )}
      </View>
      {/* The slot is always there and only its contents appear. If the spinner took
          layout space only once it mounted, the centered mark would jump up by half its
          height at 800 ms, which is the one moment the screen is trying to say "still
          working" calmly. */}
      <View style={{ marginTop: spacing.xl, height: SPINNER_SLOT }}>
        {slow ? <ActivityIndicator color={fg} /> : null}
      </View>
    </View>
  );
}

/**
 * Container: reads the device memory and renders the view.
 *
 * While the read is still in flight it draws the bare canvas and nothing else. The read
 * takes milliseconds, and showing the neutral tile for one frame before swapping to the
 * campus colors would be a visible flash on exactly the launches this screen is for.
 */
export function LoadingScreen() {
  const palette = useTheme();
  const { campus, resolved } = useLastCampus();
  if (!resolved) return <View style={{ flex: 1, backgroundColor: palette.bg }} />;
  return <LoadingScreenView campus={campus} />;
}
