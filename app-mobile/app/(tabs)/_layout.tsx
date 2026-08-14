/**
 * Tabs layout with the DESIGN.md §5 floating pill tab bar:
 * surface container radius 28, inset 12 horizontal / 8 bottom, border + card
 * shadow. Active tab = accentSoft pill behind accent Feather icon+label;
 * inactive = inkFaint Feather icon only. Tabs: Home, Yak, Messages, Orgs, Profile
 * (route dirs stay feed/yak/messages/chapter/profile for backend parity).
 */

import type { BottomTabBarProps } from "@react-navigation/bottom-tabs";
import { Redirect, Tabs } from "expo-router";
import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { Pressable, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useSession } from "@/auth";
import { AppText } from "@/components";
import { cardShadow, metrics, radii, spacing, typography, useTheme } from "@/theme";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

/** Route name → tab label + Feather icon (DESIGN §7: home/radio/message-circle/grid/user). */
const TAB_META: Record<string, { label: string; icon: FeatherIconName }> = {
  feed: { label: "Home", icon: "home" },
  yak: { label: "Yak", icon: "radio" },
  messages: { label: "Messages", icon: "message-circle" },
  chapter: { label: "Orgs", icon: "grid" },
  profile: { label: "Profile", icon: "user" },
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
          icon: "circle" as FeatherIconName,
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
                <Feather name={meta.icon} size={typography.headline.fontSize} color={palette.accent} />
                <AppText variant="micro" tone="accent" numberOfLines={1}>
                  {meta.label}
                </AppText>
              </View>
            ) : (
              <View style={{ paddingVertical: spacing.sm }}>
                <Feather name={meta.icon} size={typography.title.fontSize} color={palette.inkFaint} />
              </View>
            )}
          </Pressable>
        );
      })}
    </View>
  );
}

export default function TabsLayout() {
  // Auth guard: with real Firebase config, the tabs are members-only — an
  // unauthenticated visitor is redirected to sign-in, and a signed-in-but-
  // unregistered one (bootstrap never finished, e.g. app killed mid-onboarding)
  // is sent back to account-type instead of stranding here. Demo/mock mode
  // resolves straight to "ready" and never gates. SessionProvider owns the
  // loading timeout, so there's no local fallback needed here.
  const { status } = useSession();

  if (status === "loading") return null;
  if (status === "signedOut") return <Redirect href="/sign-in" />;
  if (status === "unregistered") return <Redirect href="/account-type" />;

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
