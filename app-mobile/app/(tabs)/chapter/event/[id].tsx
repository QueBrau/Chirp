/**
 * Event detail (DESIGN §8.7 — "the Partiful corner"): cover hero, title/when/
 * where block, an RSVP pill row (Going / Maybe / Can't — selected state in org
 * accent, gold moment on the Going count), a guest list grouped by RSVP with
 * photo avatars, and a mock Invite button. Renders inside the chapter stack's
 * OrgAccentScope (chapter/_layout.tsx), so every color here is the org's own.
 */

import { Feather } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { Image, Pressable, ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { listEvents, listRsvps, setRsvp, type EventOut, type EventRsvpOut, type RsvpStatus } from "@/api/events";
import { AppText, Button, Card, EmptyState, GradientAvatar, ListRow, SectionHeader } from "@/components";
import { MOCK_CHAPTER, MOCK_CURRENT_USER, mockUserById } from "@/mocks/data";
import { light, radii, spacing, useTheme, withAlpha } from "@/theme";

const COVER_HEIGHT = 260;

const RSVP_OPTIONS: { key: RsvpStatus; label: string }[] = [
  { key: "going", label: "Going" },
  { key: "maybe", label: "Maybe" },
  { key: "cant", label: "Can't" },
];

const GUEST_GROUP_TITLES: Record<RsvpStatus, string> = {
  going: "Going",
  maybe: "Maybe",
  cant: "Can't go",
};

export default function EventDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const palette = useTheme();

  const [event, setEvent] = useState<EventOut | null | undefined>(undefined);
  const [rsvps, setRsvps] = useState<EventRsvpOut[]>([]);

  const load = useCallback(async () => {
    const events = await listEvents(MOCK_CHAPTER.id);
    const found = events.find((candidate) => candidate.id === id) ?? null;
    setEvent(found);
    if (found) setRsvps(await listRsvps(found.id));
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleRsvp = async (status: RsvpStatus) => {
    if (!event) return;
    await setRsvp(event.id, status);
    setRsvps(await listRsvps(event.id));
  };

  if (event === undefined) {
    return <View style={{ flex: 1, backgroundColor: palette.bg }} />;
  }

  if (event === null) {
    return (
      <View style={{ flex: 1, backgroundColor: palette.bg, paddingTop: insets.top + spacing.xl, paddingHorizontal: spacing.gutter }}>
        <EmptyState title="Event not found" message="This event may have been removed." />
      </View>
    );
  }

  const host = mockUserById(event.host_id);
  const myStatus = rsvps.find((rsvp) => rsvp.user_id === MOCK_CURRENT_USER.id)?.status ?? null;

  return (
    <View style={{ flex: 1, backgroundColor: palette.bg }}>
      <ScrollView showsVerticalScrollIndicator={false} contentContainerStyle={{ paddingBottom: spacing.xxxl }}>
        <View style={{ height: COVER_HEIGHT }}>
          <Image source={{ uri: event.cover_url }} style={{ width: "100%", height: "100%" }} resizeMode="cover" />
          <View
            pointerEvents="none"
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              bottom: 0,
              height: 96,
              backgroundColor: withAlpha(light.ink, 0.28),
            }}
          />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Back"
            onPress={() => router.back()}
            hitSlop={spacing.sm}
            style={({ pressed }) => ({
              position: "absolute",
              top: insets.top + spacing.sm,
              left: spacing.gutter,
              width: 40,
              height: 40,
              borderRadius: radii.pill,
              backgroundColor: withAlpha(light.ink, 0.35),
              alignItems: "center",
              justifyContent: "center",
              opacity: pressed ? 0.8 : 1,
            })}
          >
            <Feather name="arrow-left" size={20} color={palette.onAccent} />
          </Pressable>
        </View>

        <View style={{ paddingHorizontal: spacing.gutter, paddingTop: spacing.xl, gap: spacing.xl }}>
          <View style={{ gap: spacing.sm }}>
            <AppText variant="display">{event.title}</AppText>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <Feather name="calendar" size={16} color={palette.inkFaint} />
              <AppText variant="body" tone="secondary">
                {event.date_label}
              </AppText>
            </View>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <Feather name="map-pin" size={16} color={palette.inkFaint} />
              <AppText variant="body" tone="secondary">
                {event.location}
              </AppText>
            </View>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm, marginTop: spacing.xs }}>
              <GradientAvatar name={host?.display_name ?? "Host"} size={28} photoUrl={host?.avatar_url} />
              <AppText variant="caption" tone="secondary">
                Hosted by {host?.display_name ?? "Unknown"}
              </AppText>
            </View>
          </View>

          <View style={{ gap: spacing.sm }}>
            <SectionHeader title="Are you going?" />
            <View style={{ flexDirection: "row", gap: spacing.sm }}>
              {RSVP_OPTIONS.map((option) => {
                const selected = myStatus === option.key;
                const count = rsvps.filter((rsvp) => rsvp.status === option.key).length;
                // Going count gets the "one gold moment" per §10 rule 4 — the org's
                // own accentGradient secondary stop (Sigma Chi's old gold, e.g.).
                const countColor =
                  option.key === "going"
                    ? palette.accentGradient[1]
                    : selected
                      ? palette.onAccent
                      : palette.inkFaint;

                return (
                  <Pressable
                    key={option.key}
                    accessibilityRole="button"
                    accessibilityState={{ selected }}
                    onPress={() => void handleRsvp(option.key)}
                    style={({ pressed }) => ({
                      flex: 1,
                      alignItems: "center",
                      gap: spacing.xs,
                      paddingVertical: spacing.md,
                      borderRadius: radii.pill,
                      backgroundColor: selected ? palette.accent : palette.surfaceAlt,
                      opacity: pressed ? 0.85 : 1,
                    })}
                  >
                    <AppText variant="bodyBold" tone={selected ? "onAccent" : "secondary"}>
                      {option.label}
                    </AppText>
                    <AppText variant="stat" style={{ color: countColor }}>
                      {count}
                    </AppText>
                  </Pressable>
                );
              })}
            </View>
          </View>

          <View style={{ gap: spacing.lg }}>
            {RSVP_OPTIONS.map((option) => {
              const guests = rsvps.filter((rsvp) => rsvp.status === option.key);
              if (guests.length === 0) return null;
              return (
                <View key={option.key}>
                  <SectionHeader
                    title={GUEST_GROUP_TITLES[option.key]}
                    caption={`${guests.length} ${guests.length === 1 ? "guest" : "guests"}`}
                  />
                  <Card>
                    {guests.map((rsvp, index) => {
                      const guest = mockUserById(rsvp.user_id);
                      return (
                        <ListRow
                          key={rsvp.user_id}
                          title={guest?.display_name ?? "Guest"}
                          left={
                            <GradientAvatar
                              name={guest?.display_name ?? "Guest"}
                              size={40}
                              photoUrl={guest?.avatar_url}
                            />
                          }
                          divider={index < guests.length - 1}
                        />
                      );
                    })}
                  </Card>
                </View>
              );
            })}
          </View>

          {/* Mock: no real invite flow yet — matches the join-chapter invite code system's board card. */}
          <Button label="Invite" variant="secondary" onPress={() => {}} />
        </View>
      </ScrollView>
    </View>
  );
}
