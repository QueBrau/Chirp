/**
 * Tabs layout with the DESIGN.md §5 floating pill tab bar:
 * surface container radius 28, inset 12 horizontal / 8 bottom, border + card
 * shadow. Active tab = accentSoft pill behind accent glyph+label; inactive =
 * inkFaint glyph only. Tabs: Home, Yak, Messages, Orgs, Profile
 * (route dirs stay feed/yak/messages/chapter/profile for backend parity).
 */

import type { BottomTabBarProps } from "@react-navigation/bottom-tabs";
import { Tabs } from "expo-router";
import { Pressable, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { AppText } from "@/components";
import { cardShadow, metrics, radii, spacing, typography, useTheme } from "@/theme";

/** Route name → tab label + glyph (no icon library in the scaffold). */
const TAB_META: Record<string, { label: string; glyph: string }> = {
  feed: { label: "Home", glyph: "⌂" },
  yak: { label: "Yak", glyph: "◎" },
  messages: { label: "Messages", glyph: "✉" },
  chapter: { label: "Orgs", glyph: "⬡" },
  profile: { label: "Profile", glyph: "◉" },
};

function FloatingTabBar({ state, descriptors, navigation }: BottomTabBarProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();

  return (
    <View
      style={{
        position: "absolute",
        left: metrics.tabBarInsetX,
        right: metrics.tabBarInsetX,
        bottom: Math.max(insets.bottom, metrics.tabBarInsetBottom),
        flexDirection: "row",
        alignItems: "center",
        backgroundColor: palette.surface,
        borderRadius: radii.tabBar,
        borderWidth: 1,
        borderColor: palette.border,
        paddingVertical: spacing.sm,
        paddingHorizontal: spacing.sm,
        ...cardShadow(palette),
      }}
    >
      {state.routes.map((route, index) => {
        const focused = state.index === index;
        const { options } = descriptors[route.key];
        const meta = TAB_META[route.name] ?? {
          label: options.title ?? route.name,
          glyph: "•",
        };

        const onPress = () => {
          const event = navigation.emit({
            type: "tabPress",
            target: route.key,
            canPreventDefault: true,
          });
          if (!focused && !event.defaultPrevented) {
            navigation.navigate(route.name);
          }
        };

        return (
          <Pressable
            key={route.key}
            accessibilityRole="button"
            accessibilityState={{ selected: focused }}
            accessibilityLabel={meta.label}
            onPress={onPress}
            style={{ flex: focused ? 1.7 : 1, alignItems: "center" }}
          >
            {focused ? (
              <View
                style={{
                  flexDirection: "row",
                  alignItems: "center",
                  gap: spacing.xs,
                  backgroundColor: palette.accentSoft,
                  borderRadius: radii.pill,
                  paddingHorizontal: spacing.md,
                  paddingVertical: spacing.sm,
                }}
              >
                <Text style={{ color: palette.accent, fontSize: typography.headline.fontSize }}>
                  {meta.glyph}
                </Text>
                <AppText variant="micro" tone="accent" numberOfLines={1}>
                  {meta.label}
                </AppText>
              </View>
            ) : (
              <View style={{ paddingVertical: spacing.sm }}>
                <Text style={{ color: palette.inkFaint, fontSize: typography.title.fontSize }}>
                  {meta.glyph}
                </Text>
              </View>
            )}
          </Pressable>
        );
      })}
    </View>
  );
}

export default function TabsLayout() {
  return (
    <Tabs
      tabBar={(props) => <FloatingTabBar {...props} />}
      screenOptions={{ headerShown: false }}
    >
      <Tabs.Screen name="feed" options={{ title: "Home" }} />
      <Tabs.Screen name="yak" options={{ title: "Yak" }} />
      <Tabs.Screen name="messages" options={{ title: "Messages" }} />
      <Tabs.Screen name="chapter" options={{ title: "Orgs" }} />
      <Tabs.Screen name="profile" options={{ title: "Profile" }} />
    </Tabs>
  );
}
