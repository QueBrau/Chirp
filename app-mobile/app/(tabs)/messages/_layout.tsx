/** Messages tab stack (list + thread) — headerless, canvas bg behind transitions. */

import { Stack } from "expo-router";

import { useTheme } from "@/theme";

export default function MessagesLayout() {
  const palette = useTheme();
  return (
    <Stack
      screenOptions={{ headerShown: false, contentStyle: { backgroundColor: palette.bg } }}
    />
  );
}
