/** Root layout: navigation theme wired to the Chirp palette (light/dark via system), canvas bg token applied. */

import {
  DarkTheme,
  DefaultTheme,
  ThemeProvider,
  type Theme,
} from "@react-navigation/native";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useColorScheme } from "react-native";

import { dark, light } from "@/theme";

/**
 * Mock auth state — flips which route group app/index.tsx lands on.
 * TODO(milestone-1): replace with real Firebase Auth session state.
 */
export const IS_SIGNED_IN = true;

export default function RootLayout() {
  const scheme = useColorScheme();
  const palette = scheme === "dark" ? dark : light;
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
