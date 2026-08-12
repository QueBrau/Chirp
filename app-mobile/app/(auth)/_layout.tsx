/** Auth group layout: plain stack, screens draw their own headers via <Screen>. */

import { Stack } from "expo-router";

export default function AuthLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}
