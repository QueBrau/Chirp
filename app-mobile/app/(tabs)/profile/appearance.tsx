/**
 * Profile → Settings → Appearance (DESIGN §8.5): the user picks what drives the
 * app's accent color and background. Live preview card up top, then two section
 * cards (Accent color / Background) with tappable swatch rows. Changes apply
 * instantly app-wide via AppearanceProvider — mock persistence (context state)
 * for now, real per-user prefs later.
 */

import { Feather } from "@expo/vector-icons";
import { Pressable, useColorScheme, View } from "react-native";

import { AppText, Button, Card, GradientAvatar, ListRow, Screen } from "@/components";
import { MOCK_CAMPUS, MOCK_CURRENT_USER } from "@/mocks/data";
import {
  radii,
  resolvePalette,
  spacing,
  typography,
  useAppearance,
  useTheme,
  type AccentSource,
  type BackgroundStyle,
} from "@/theme";

const ACCENT_OPTIONS: readonly { source: AccentSource; label: string }[] = [
  { source: "campusPrimary", label: "Campus primary" },
  { source: "campusSecondary", label: "Campus secondary" },
  { source: "chirp", label: "Chirp violet" },
];

const BACKGROUND_OPTIONS: readonly { style: BackgroundStyle; label: string; caption: string }[] = [
  { style: "system", label: "System", caption: "Default light/dark, neutral canvas" },
  { style: "campusTint", label: "Campus tint", caption: `A subtle wash of ${MOCK_CAMPUS.name}'s color` },
];

/** Tappable color circle for the Accent color section — Feather check on the active one. */
function AccentSwatch({
  color,
  label,
  active,
  onPress,
}: {
  color: string;
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ selected: active }}
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({ alignItems: "center", gap: spacing.sm, opacity: pressed ? 0.8 : 1 })}
    >
      <View
        style={{
          width: 48,
          height: 48,
          borderRadius: radii.pill,
          backgroundColor: color,
          alignItems: "center",
          justifyContent: "center",
          borderWidth: active ? 0 : 1,
          borderColor: palette.border,
        }}
      >
        {active ? <Feather name="check" size={typography.title.fontSize} color={palette.onAccent} /> : null}
      </View>
      <AppText variant="caption" tone={active ? "primary" : "secondary"} style={{ textAlign: "center" }}>
        {label}
      </AppText>
    </Pressable>
  );
}

/** Tappable row for the Background section — swatch preview + label/caption + Feather check when active. */
function BackgroundRow({
  swatch,
  label,
  caption,
  active,
  onPress,
  divider,
}: {
  swatch: string;
  label: string;
  caption: string;
  active: boolean;
  onPress: () => void;
  divider: boolean;
}) {
  const palette = useTheme();
  return (
    <ListRow
      title={label}
      subtitle={caption}
      divider={divider}
      onPress={onPress}
      left={
        <View
          style={{
            width: spacing.xxl,
            height: spacing.xxl,
            borderRadius: radii.pill,
            backgroundColor: swatch,
            borderWidth: 1,
            borderColor: palette.border,
          }}
        />
      }
      right={
        active ? <Feather name="check" size={typography.headline.fontSize} color={palette.accent} /> : undefined
      }
    />
  );
}

/** 36 circular action chip, matching the Home feed's post action row (DESIGN §7). */
function PreviewActionChip({
  name,
  active,
}: {
  name: "heart" | "message-circle" | "send";
  active: boolean;
}) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: 36,
        height: 36,
        borderRadius: radii.pill,
        backgroundColor: active ? palette.accentSoft : palette.surfaceAlt,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Feather name={name} size={16} color={active ? palette.accent : palette.inkFaint} />
    </View>
  );
}

export default function AppearanceScreen() {
  const scheme = useColorScheme();
  const mode: "light" | "dark" = scheme === "dark" ? "dark" : "light";
  const { prefs, setPrefs, campusColors } = useAppearance();

  const selectAccent = (source: AccentSource) =>
    setPrefs((current) => ({ ...current, accentSource: source }));
  const selectBackground = (style: BackgroundStyle) =>
    setPrefs((current) => ({ ...current, backgroundStyle: style }));

  return (
    <Screen title="Appearance" subtitle="Your campus colors, your call">
      {/* Live preview: a mini post card + button rendered with the current prefs. */}
      <Card style={{ marginBottom: spacing.xl }}>
        <AppText variant="micro" tone="tertiary" style={{ marginBottom: spacing.md }}>
          Preview
        </AppText>
        <View style={{ gap: spacing.md }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
            <GradientAvatar name={MOCK_CURRENT_USER.display_name} size={40} />
            <View style={{ gap: spacing.xs }}>
              <AppText variant="headline">{MOCK_CURRENT_USER.display_name}</AppText>
              <AppText variant="caption" tone="tertiary">
                just now
              </AppText>
            </View>
          </View>
          <AppText>New colors, who dis? Go {MOCK_CAMPUS.name.split(" ")[0]}.</AppText>
          <View style={{ flexDirection: "row", gap: spacing.sm }}>
            <PreviewActionChip name="heart" active />
            <PreviewActionChip name="message-circle" active={false} />
            <PreviewActionChip name="send" active={false} />
          </View>
          <Button label="Post" variant="primary" />
        </View>
      </Card>

      <Card style={{ marginBottom: spacing.md }}>
        <AppText variant="title" style={{ marginBottom: spacing.lg }}>
          Accent color
        </AppText>
        <View style={{ flexDirection: "row", gap: spacing.xl }}>
          {ACCENT_OPTIONS.map((option) => {
            const color = resolvePalette(mode, { ...prefs, accentSource: option.source }, campusColors).accent;
            return (
              <AccentSwatch
                key={option.source}
                color={color}
                label={option.label}
                active={prefs.accentSource === option.source}
                onPress={() => selectAccent(option.source)}
              />
            );
          })}
        </View>
      </Card>

      <Card>
        <AppText variant="title" style={{ marginBottom: spacing.sm }}>
          Background
        </AppText>
        <View>
          {BACKGROUND_OPTIONS.map((option, index) => {
            const swatch = resolvePalette(
              mode,
              { ...prefs, backgroundStyle: option.style },
              campusColors,
            ).bg;
            return (
              <BackgroundRow
                key={option.style}
                swatch={swatch}
                label={option.label}
                caption={option.caption}
                active={prefs.backgroundStyle === option.style}
                onPress={() => selectBackground(option.style)}
                divider={index < BACKGROUND_OPTIONS.length - 1}
              />
            );
          })}
        </View>
      </Card>
    </Screen>
  );
}
