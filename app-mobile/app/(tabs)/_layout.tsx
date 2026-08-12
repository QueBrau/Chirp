/** Tabs layout: Feed, Yak, Messages, Chapter, Profile (SPEC §1 navigation). */

import { Tabs } from "expo-router";
import { Text } from "react-native";

import { typography, useTheme } from "@/theme";

/** Minimal glyph tab icon — no icon library in the scaffold; swap for real icons later. */
function TabGlyph({ glyph, color }: { glyph: string; color: string }) {
  return <Text style={{ color, fontSize: typography.title.fontSize }}>{glyph}</Text>;
}

export default function TabsLayout() {
  const palette = useTheme();

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: palette.accent,
        tabBarInactiveTintColor: palette.textTertiary,
        tabBarStyle: { backgroundColor: palette.surface, borderTopColor: palette.border },
      }}
    >
      <Tabs.Screen
        name="feed"
        options={{
          title: "Feed",
          tabBarIcon: ({ color }) => <TabGlyph glyph={"▤"} color={color} />,
        }}
      />
      <Tabs.Screen
        name="yak"
        options={{
          title: "Yak",
          tabBarIcon: ({ color }) => <TabGlyph glyph={"◎"} color={color} />,
        }}
      />
      <Tabs.Screen
        name="messages"
        options={{
          title: "Messages",
          tabBarIcon: ({ color }) => <TabGlyph glyph={"✉︎"} color={color} />,
        }}
      />
      <Tabs.Screen
        name="chapter"
        options={{
          title: "Chapter",
          tabBarIcon: ({ color }) => <TabGlyph glyph={"⌂"} color={color} />,
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: "Profile",
          tabBarIcon: ({ color }) => <TabGlyph glyph={"◉"} color={color} />,
        }}
      />
    </Tabs>
  );
}
