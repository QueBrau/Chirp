/** Messages tab stack: conversation list + thread view. */

import { Stack } from "expo-router";

export default function MessagesLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}
