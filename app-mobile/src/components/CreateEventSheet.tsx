/**
 * CreateEventSheet (DESIGN §8.7): the create-event bottom sheet - title, when, where,
 * who can see it, and a cover choice. Matches the CreateSheet pattern (backdrop +
 * rounded sheet + drag handle).
 *
 * NO NATIVE DATE PICKER, AND THAT IS A DELIBERATE CONSTRAINT RATHER THAN A SHORTCUT.
 * @react-native-community/datetimepicker is a native module, so adding it would
 * invalidate the current EAS dev build and block every device check in the repo until
 * a new one is cut (exactly the trap board c39 and c166 both record). Structured text
 * fields parse to a real instant today; swapping in a wheel picker later changes this
 * file only, because what leaves here is already an ISO string.
 *
 * THE VISIBILITY ROW SAYS WHAT IT DOES IN PLAIN WORDS. A host picking "Anyone" is
 * publishing a real party's address and time to people with no account, which is a
 * hole through the .edu gate every other campus surface enforces (c88). c198 agreed to
 * offer it; it did not agree to offer it quietly, so the caption under the selected
 * tier states the consequence and the public one is never preselected.
 */

import { Feather } from "@expo/vector-icons";
import { useState } from "react";
import { Image, Modal, Pressable, ScrollView, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { EventVisibility } from "@/api/events";
import { light, radii, spacing, typography, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

export interface CreateEventInput {
  title: string;
  starts_at: string;
  ends_at?: string | null;
  location: string;
  cover_url: string;
  description?: string | null;
  visibility: EventVisibility;
}

export interface CreateEventSheetProps {
  visible: boolean;
  onClose: () => void;
  onCreate: (input: CreateEventInput) => void;
  /**
   * Prefill for EDIT mode. The same sheet serves create and edit rather than a second
   * near-identical component: the fields, the parsing and the visibility warning are
   * the parts most likely to drift apart, and they are exactly the parts a copy would
   * duplicate. Passing this switches the heading and the submit label.
   */
  initial?: CreateEventInput | null;
  heading?: string;
  submitLabel?: string;
}

/** Cover choices (§8.7 "cover choice") - distinct seeds from the org feed's photo posts. */
const COVER_SEEDS = ["sigchi-cover-a", "sigchi-cover-b", "sigchi-cover-c", "sigchi-cover-d"];

function coverUrl(seed: string): string {
  return `https://picsum.photos/seed/${seed}/300/300`;
}

/**
 * Narrowest first, and the order on screen is the order of exposure - a host reading
 * left to right is reading "fewer people" to "more people".
 */
const VISIBILITY_TIERS: { key: EventVisibility; label: string; caption: string }[] = [
  {
    key: "chapter",
    label: "Chapter",
    caption: "Only active members of your chapter can see this.",
  },
  {
    key: "campus",
    label: "Campus",
    caption: "Any verified student at your school can see this and RSVP.",
  },
  {
    key: "verified",
    label: "Any student",
    caption: "Verified students at any school can see this - use it for sister chapters.",
  },
  {
    key: "public",
    label: "Anyone",
    caption:
      "Anyone with the link can see the title, time and address, with no account and no student check. Your guest list stays private.",
  },
];

function FieldLabel({ children }: { children: string }) {
  return (
    <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
      {children}
    </AppText>
  );
}

/**
 * Combine a YYYY-MM-DD and an HH:MM entered in the host's own timezone into an ISO
 * instant, or null if either is unusable.
 *
 * Constructed via the Date(y, m, d, h, min) constructor rather than by string-concat +
 * parse: the concat form ("2026-09-27T19:00") is interpreted as UTC by some engines and
 * as local by others, which would silently shift every party by the offset.
 */
export function toIsoInstant(day: string, time: string): string | null {
  const dayMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day.trim());
  const timeMatch = /^(\d{1,2}):(\d{2})$/.exec(time.trim());
  if (!dayMatch || !timeMatch) return null;
  const [, y, m, d] = dayMatch;
  const [, hh, mm] = timeMatch;
  const hours = Number(hh);
  const minutes = Number(mm);
  if (hours > 23 || minutes > 59) return null;
  const date = new Date(Number(y), Number(m) - 1, Number(d), hours, minutes, 0, 0);
  if (Number.isNaN(date.getTime())) return null;
  // Reject a rolled-over date (Feb 31 becomes Mar 3) rather than accepting the roll.
  if (date.getMonth() !== Number(m) - 1 || date.getDate() !== Number(d)) return null;
  return date.toISOString();
}

/**
 * Split an ISO instant back into the local YYYY-MM-DD and HH:MM the fields hold.
 *
 * Built from the LOCAL getters, not from slicing the ISO string: the string is UTC, and
 * slicing it would show a host in Greensboro the UTC date, which for an evening party
 * is tomorrow. This is the exact inverse of toIsoInstant.
 */
export function fromIsoInstant(iso: string): { day: string; time: string } {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return { day: "", time: "" };
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    day: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  };
}

export function CreateEventSheet({
  visible,
  onClose,
  onCreate,
  initial = null,
  heading = "New event",
  submitLabel = "Create event",
}: CreateEventSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const seededStart = initial ? fromIsoInstant(initial.starts_at) : null;
  const seededEnd = initial?.ends_at ? fromIsoInstant(initial.ends_at) : null;

  const [title, setTitle] = useState(initial?.title ?? "");
  const [day, setDay] = useState(seededStart?.day ?? "");
  const [startTime, setStartTime] = useState(seededStart?.time ?? "");
  const [endTime, setEndTime] = useState(seededEnd?.time ?? "");
  const [location, setLocation] = useState(initial?.location ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [visibility, setVisibility] = useState<EventVisibility>(initial?.visibility ?? "chapter");
  const [cover, setCover] = useState(COVER_SEEDS[0]);

  const reset = () => {
    setTitle(initial?.title ?? "");
    setDay(seededStart?.day ?? "");
    setStartTime(seededStart?.time ?? "");
    setEndTime(seededEnd?.time ?? "");
    setLocation(initial?.location ?? "");
    setDescription(initial?.description ?? "");
    setVisibility(initial?.visibility ?? "chapter");
    setCover(COVER_SEEDS[0]);
  };

  const close = () => {
    reset();
    onClose();
  };

  const startsAt = toIsoInstant(day, startTime);
  const endsAt = endTime.trim().length > 0 ? toIsoInstant(day, endTime) : null;
  // An end time that parses but lands before the start is rejected here rather than
  // being sent - the server refuses it with a 422, and a round trip to learn that the
  // party ends before it begins is a worse way to find out.
  const endIsValid = endTime.trim().length === 0 || (endsAt !== null && startsAt !== null && endsAt > startsAt);
  const canSubmit =
    title.trim().length > 0 && location.trim().length > 0 && startsAt !== null && endIsValid;

  const submit = () => {
    if (!canSubmit || startsAt === null) return;
    onCreate({
      title: title.trim(),
      starts_at: startsAt,
      ends_at: endsAt,
      location: location.trim(),
      cover_url: initial ? initial.cover_url : coverUrl(cover),
      description: description.trim().length > 0 ? description.trim() : null,
      visibility,
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

  const selectedTier = VISIBILITY_TIERS.find((t) => t.key === visibility);

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={close}>
      {/* c131: NOT accessibilityRole="button" - same fix as CreateSheet.tsx. This
          backdrop was a byte-for-byte copy of that one, so it carried the identical
          bug: react-native-web maps accessibilityRole="button" to a literal <button>,
          and this backdrop wraps every real control in the form, each ALSO
          accessibilityRole="button" - a <button> nested inside a <button>. onPress
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
            maxHeight: "90%",
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

          {/* c141: matches CreateSheet.tsx's fix, same cause - c131 correctly took
              accessibilityRole="button" off the backdrop, which also took away the
              only labeled dismiss control assistive tech had. */}
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Close"
            onPress={close}
            hitSlop={spacing.sm}
            style={{
              position: "absolute",
              top: spacing.lg,
              right: spacing.gutter,
              width: 28,
              height: 28,
              borderRadius: radii.pill,
              backgroundColor: palette.surfaceAlt,
              alignItems: "center",
              justifyContent: "center",
              zIndex: 1,
            }}
          >
            <Feather name="x" size={16} color={palette.inkSecondary} />
          </Pressable>

          <ScrollView
            showsVerticalScrollIndicator={false}
            contentContainerStyle={{ gap: spacing.lg, paddingBottom: spacing.lg }}
          >
            <AppText variant="title">{heading}</AppText>

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
                value={day}
                onChangeText={setDay}
                placeholder="YYYY-MM-DD"
                placeholderTextColor={palette.inkFaint}
                keyboardType="numbers-and-punctuation"
                style={inputStyle}
              />
            </View>

            <View style={{ flexDirection: "row", gap: spacing.sm }}>
              <View style={{ flex: 1 }}>
                <FieldLabel>Starts</FieldLabel>
                <TextInput
                  value={startTime}
                  onChangeText={setStartTime}
                  placeholder="19:00"
                  placeholderTextColor={palette.inkFaint}
                  keyboardType="numbers-and-punctuation"
                  style={inputStyle}
                />
              </View>
              <View style={{ flex: 1 }}>
                <FieldLabel>Ends (optional)</FieldLabel>
                <TextInput
                  value={endTime}
                  onChangeText={setEndTime}
                  placeholder="23:00"
                  placeholderTextColor={palette.inkFaint}
                  keyboardType="numbers-and-punctuation"
                  style={inputStyle}
                />
              </View>
            </View>

            {!endIsValid ? (
              <AppText variant="caption" tone="danger">
                The end time has to come after the start time.
              </AppText>
            ) : null}

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
              <FieldLabel>Details (optional)</FieldLabel>
              <TextInput
                value={description}
                onChangeText={setDescription}
                placeholder="Dress code, parking, what to bring"
                placeholderTextColor={palette.inkFaint}
                multiline
                style={{ ...inputStyle, minHeight: 72, textAlignVertical: "top" }}
              />
            </View>

            <View>
              <FieldLabel>Who can see it</FieldLabel>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
                {VISIBILITY_TIERS.map((tier) => {
                  const selected = tier.key === visibility;
                  return (
                    <Pressable
                      key={tier.key}
                      accessibilityRole="button"
                      accessibilityState={{ selected }}
                      onPress={() => setVisibility(tier.key)}
                      style={{
                        paddingHorizontal: spacing.lg,
                        paddingVertical: spacing.sm,
                        borderRadius: radii.pill,
                        backgroundColor: selected ? palette.accent : palette.surfaceAlt,
                      }}
                    >
                      <AppText
                        variant="caption"
                        style={{ color: selected ? palette.onAccent : palette.inkSecondary }}
                      >
                        {tier.label}
                      </AppText>
                    </Pressable>
                  );
                })}
              </View>
              {selectedTier ? (
                <AppText
                  variant="caption"
                  tone={visibility === "public" ? "danger" : "tertiary"}
                  style={{ marginTop: spacing.sm }}
                >
                  {selectedTier.caption}
                </AppText>
              ) : null}
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
                      <Image
                        source={{ uri: coverUrl(seed) }}
                        style={{ width: "100%", height: "100%" }}
                      />
                    </Pressable>
                  );
                })}
              </View>
            </View>

            <Button label={submitLabel} onPress={submit} disabled={!canSubmit} />
          </ScrollView>
        </Pressable>
      </Pressable>
    </Modal>
  );
}
