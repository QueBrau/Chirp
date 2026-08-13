/**
 * CreateSheet (DESIGN §7): mock bottom sheet opened by the Home FAB — Photo /
 * Video / Text options as ListRows with Feather icons. Mock actions: each
 * option just closes the sheet (no real capture/composer yet).
 */

import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { Modal, Pressable, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { light, radii, spacing, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { ListRow } from "./ListRow";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

export interface CreateSheetProps {
  visible: boolean;
  onClose: () => void;
}

const OPTIONS: { key: string; icon: FeatherIconName; title: string; subtitle: string }[] = [
  { key: "photo", icon: "image", title: "Photo", subtitle: "Share a photo with your feed" },
  { key: "video", icon: "video", title: "Video", subtitle: "Share a short video" },
  { key: "text", icon: "type", title: "Text", subtitle: "Post a text update" },
];

export function CreateSheet({ visible, onClose }: CreateSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Close"
        onPress={onClose}
        style={{ flex: 1, backgroundColor: withAlpha(light.ink, 0.4), justifyContent: "flex-end" }}
      >
        {/* Inner Pressable with no onPress: swallows taps so they don't bubble to the backdrop close. */}
        <Pressable
          style={{
            backgroundColor: palette.surface,
            borderTopLeftRadius: radii.card,
            borderTopRightRadius: radii.card,
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.lg,
            paddingBottom: insets.bottom + spacing.lg,
          }}
        >
          <View
            style={{
              alignSelf: "center",
              width: 40,
              height: 4,
              borderRadius: radii.pill,
              backgroundColor: palette.border,
              marginBottom: spacing.lg,
            }}
          />
          <AppText variant="title" style={{ marginBottom: spacing.md }}>
            Create
          </AppText>
          {OPTIONS.map((option, index) => (
            <ListRow
              key={option.key}
              title={option.title}
              subtitle={option.subtitle}
              divider={index < OPTIONS.length - 1}
              onPress={onClose}
              left={
                <View
                  style={{
                    width: 40,
                    height: 40,
                    borderRadius: radii.avatar,
                    backgroundColor: palette.accentSoft,
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Feather name={option.icon} size={18} color={palette.accent} />
                </View>
              }
            />
          ))}
        </Pressable>
      </Pressable>
    </Modal>
  );
}
