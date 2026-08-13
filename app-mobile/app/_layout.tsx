/**
 * Root layout: navigation theme wired to the Chirp palette, which now resolves
 * through AppearanceProvider (DESIGN §8.5 campus theming) — light/dark still
 * follows system, accent + canvas bg follow the user's appearance prefs.
 */

import {
  DarkTheme,
  DefaultTheme,
  ThemeProvider,
  type Theme,
} from "@react-navigation/native";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { useColorScheme } from "react-native";

import { hasFirebaseConfig, onIdTokenChanged } from "@/auth";
import { AppearanceProvider, useTheme } from "@/theme";
import { MOCK_CAMPUS_COLORS, mockAppearancePrefs } from "@/mocks/data";

/** Consumes useTheme() — must render inside AppearanceProvider. */
function RootLayoutNav() {
  const scheme = useColorScheme();
  const palette = useTheme();
  const base = scheme === "dark" ? DarkTheme : DefaultTheme;

  const navTheme: Theme = {
    ...base,
    colors: {
      ...base.colors,
      primary: palette.accent,
      background: palette.bg,
      card: palette.surface,
      text: palette.ink,
      border: palette.border,
      notification: palette.danger,
    },
  };

  return (
    <ThemeProvider value={navTheme}>
      <StatusBar style="auto" />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: palette.bg },
        }}
      >
        <Stack.Screen name="(auth)" />
        <Stack.Screen name="(tabs)" />
      </Stack>
    </ThemeProvider>
  );
}

export default function RootLayout() {
  // Keep src/api/client's bearer token fresh across the ~1hr Firebase ID token
  // expiry. No-op in mock/demo mode (hasFirebaseConfig() false) — see SECURITY-REVIEW
  // finding 11.
  useEffect(() => {
    if (!hasFirebaseConfig()) return;
    return onIdTokenChanged();
  }, []);

  return (
    <AppearanceProvider campusColors={MOCK_CAMPUS_COLORS} initialPrefs={mockAppearancePrefs}>
      <RootLayoutNav />
    </AppearanceProvider>
  );
}
