/** Feed (Home) tab stack — headerless, canvas bg behind transitions. */

import { Stack } from "expo-router";

import { useTheme } from "@/theme";

export default function FeedLayout() {
  const palette = useTheme();
  return (
    <Stack
      screenOptions={{ headerShown: false, contentStyle: { backgroundColor: palette.bg } }}
    />
  );
}
