/**
 * CreateEventSheet (DESIGN §8.7): mock create-event bottom sheet — title, date,
 * location rows plus a cover-photo choice row. Matches the CreateSheet pattern
 * (backdrop + rounded sheet + drag handle). Mock: no real calendar picker —
 * date is a free-text label (e.g. "Sat, Sep 27 · 7:00 PM"), closes on submit.
 */

import { useState } from "react";
import { Image, Modal, Pressable, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { light, radii, spacing, typography, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

export interface CreateEventInput {
  title: string;
  date_label: string;
  location: string;
  cover_url: string;
}

export interface CreateEventSheetProps {
  visible: boolean;
  onClose: () => void;
  onCreate: (input: CreateEventInput) => void;
}

/** Cover choices (§8.7 "cover choice") — distinct seeds from the org feed's photo posts. */
const COVER_SEEDS = ["sigchi-cover-a", "sigchi-cover-b", "sigchi-cover-c", "sigchi-cover-d"];

function coverUrl(seed: string): string {
  return `https://picsum.photos/seed/${seed}/300/300`;
}

function FieldLabel({ children }: { children: string }) {
  return (
    <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
      {children}
    </AppText>
  );
}

export function CreateEventSheet({ visible, onClose, onCreate }: CreateEventSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [title, setTitle] = useState("");
  const [dateLabel, setDateLabel] = useState("");
  const [location, setLocation] = useState("");
  const [cover, setCover] = useState(COVER_SEEDS[0]);

  const reset = () => {
    setTitle("");
    setDateLabel("");
    setLocation("");
    setCover(COVER_SEEDS[0]);
  };

  const close = () => {
    reset();
    onClose();
  };

  const canSubmit = title.trim().length > 0 && location.trim().length > 0;

  const submit = () => {
    if (!canSubmit) return;
    onCreate({
      title: title.trim(),
      date_label: dateLabel.trim().length > 0 ? dateLabel.trim() : "Date TBD",
      location: location.trim(),
      cover_url: coverUrl(cover),
    });
    reset();
    onClose();
  };

  const inputStyle = {
    ...typography.body,
    color: palette.ink,
    backgroundColor: palette.surfaceAlt,
    borderRadius: radii.input,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  };

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={close}>
      {/* c131: NOT accessibilityRole="button" — same fix as CreateSheet.tsx. This
          backdrop was a byte-for-byte copy of that one, so it carried the identical
          bug: react-native-web maps accessibilityRole="button" to a literal <button>,
          and this backdrop wraps every real control in the form, each ALSO
          accessibilityRole="button" — a <button> nested inside a <button>. onPress
          alone keeps it tappable without the semantic role. */}
      <Pressable
        onPress={close}
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
            gap: spacing.lg,
          }}
        >
          <View
            style={{
              alignSelf: "center",
              width: 40,
              height: 4,
              borderRadius: radii.pill,
              backgroundColor: palette.border,
            }}
          />
          <AppText variant="title">New event</AppText>

          <View>
            <FieldLabel>Title</FieldLabel>
            <TextInput
              value={title}
              onChangeText={setTitle}
              placeholder="e.g. Founders Day Formal"
              placeholderTextColor={palette.inkFaint}
              style={inputStyle}
            />
          </View>

          <View>
            <FieldLabel>Date</FieldLabel>
            <TextInput
              value={dateLabel}
              onChangeText={setDateLabel}
              placeholder="e.g. Sat, Sep 27 · 7:00 PM"
              placeholderTextColor={palette.inkFaint}
              style={inputStyle}
            />
          </View>

          <View>
            <FieldLabel>Location</FieldLabel>
            <TextInput
              value={location}
              onChangeText={setLocation}
              placeholder="e.g. Blandwood Mansion"
              placeholderTextColor={palette.inkFaint}
              style={inputStyle}
            />
          </View>

          <View>
            <FieldLabel>Cover</FieldLabel>
            <View style={{ flexDirection: "row", gap: spacing.sm }}>
              {COVER_SEEDS.map((seed) => {
                const selected = seed === cover;
                return (
                  <Pressable
                    key={seed}
                    accessibilityRole="button"
                    accessibilityState={{ selected }}
                    onPress={() => setCover(seed)}
                    style={{
                      width: 56,
                      height: 56,
                      borderRadius: radii.thumb,
                      overflow: "hidden",
                      borderWidth: selected ? 2 : 0,
                      borderColor: palette.accent,
                    }}
                  >
                    <Image source={{ uri: coverUrl(seed) }} style={{ width: "100%", height: "100%" }} />
                  </Pressable>
                );
              })}
            </View>
          </View>

          <Button label="Create event" onPress={submit} disabled={!canSubmit} />
        </Pressable>
      </Pressable>
    </Modal>
  );
}
