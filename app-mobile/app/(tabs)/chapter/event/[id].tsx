/**
 * Event detail (DESIGN §8.7 - "the Partiful corner"): cover hero, title/when/where
 * block, an RSVP pill row (Going / Maybe / Can't - selected state in org accent, gold
 * moment on the Going count), a guest list grouped by RSVP with photo avatars, and -
 * since c198 - a real invite flow plus host edit/cancel.
 *
 * TWO KINDS OF VIEWER REACH THIS SCREEN AND THEY ARE NOT THE SAME (c198). A MEMBER of
 * the hosting chapter gets the roster, the invite button and, if they host or sit on
 * the e-board, edit and cancel. An INVITED OUTSIDER - the whole point of invites - is
 * allowed to read the event and answer it, but is not in the chapter, so
 * `listMembers()` would 403 for them. The roster fetch is therefore gated on
 * membership rather than attempted-and-caught: a 403 swallowed by a try/catch looks
 * exactly like an empty chapter, and the screen would silently render every guest as
 * "Guest" for members too if the gate ever moved.
 *
 * Renders inside the chapter stack's OrgAccentScope (chapter/_layout.tsx), so every
 * color here is the org's own.
 */

import { Feather } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { Image, Modal, Pressable, ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { listMembers, type MemberOut } from "@/api/chapters";
import {
  cancelEvent,
  getEvent,
  getRsvpCounts,
  inviteToEvent,
  listEventInvites,
  listRsvps,
  setRsvp,
  updateEvent,
  type EventInviteOut,
  type EventOut,
  type EventRsvpCountsOut,
  type EventRsvpOut,
  type RsvpStatus,
} from "@/api/events";
import { useSession } from "@/auth";
import {
  AppText,
  Button,
  Card,
  Chip,
  CreateEventSheet,
  EmptyState,
  GradientAvatar,
  ListRow,
  SectionHeader,
  type CreateEventInput,
} from "@/components";
import { ApiError } from "@/api/client";
import { confirmAction, showApiError } from "@/lib/alert";
import { eventWhen } from "@/lib/dates";
import { findMember } from "@/lib/roster";
import { useOwnChapter } from "@/org/OwnChapterProvider";
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

/** Roles that may edit or cancel somebody else's event. Mirrors backend EBOARD. */
/** Loading: the fetch is in flight. Loaded: it settled - `event` is the event, or null
 * because it is genuinely gone or not shared with this viewer. Error: the fetch itself
 * failed, which must never be presented as either of those (c312). */
type LoadState = "loading" | "loaded" | "error";

const EBOARD_ROLES = ["president", "vice_president", "treasurer", "secretary", "historian"];

/** Plain-language label for who can see this. Matches CreateEventSheet's tier names. */
const VISIBILITY_LABELS: Record<EventOut["visibility"], string> = {
  chapter: "Chapter only",
  campus: "Anyone at your school",
  verified: "Any verified student",
  public: "Anyone with the link",
};

export default function EventDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const palette = useTheme();
  const { user } = useSession();
  const { sessionStatus, membership, chapterLoading } = useOwnChapter();

  const [event, setEvent] = useState<EventOut | null | undefined>(undefined);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [rsvps, setRsvpsState] = useState<EventRsvpOut[]>([]);
  const [invites, setInvites] = useState<EventInviteOut[]>([]);
  const [counts, setCounts] = useState<EventRsvpCountsOut | null>(null);
  const [members, setMembers] = useState<MemberOut[]>([]);
  const [inviting, setInviting] = useState(false);
  const [editing, setEditing] = useState(false);

  const load = useCallback(async () => {
    // Fetch the event by id rather than finding it in the chapter's list: an invited
    // outsider has no chapter list to find it in, and that is exactly who invites exist
    // for.
    const found = await getEvent(String(id));
    setEvent(found);

    // Membership in THIS event's chapter, not merely having a chapter of your own.
    const isMember = membership?.chapter_id === found.chapter_id;
    // c275: the /guests wrapper split into paged routes plus a counts endpoint.
    // Each call fails soft INDEPENDENTLY - a guest-list failure must not blank the
    // event header, and a counts failure falls back to list-derived numbers below.
    // limit 200 is the route cap; the chips read TRUE numbers from counts, so a
    // 200-row page under a bigger event shows a complete-enough roster while the
    // headcount stays exact.
    const [rsvpPage, invitePage, headcounts, roster] = await Promise.all([
      listRsvps(found.id, { limit: 200 }).catch(() => [] as EventRsvpOut[]),
      listEventInvites(found.id, { limit: 200 }).catch(() => [] as EventInviteOut[]),
      getRsvpCounts(found.id).catch(() => null),
      isMember ? listMembers(found.chapter_id) : Promise.resolve<MemberOut[]>([]),
    ]);
    setRsvpsState(rsvpPage);
    setInvites(invitePage);
    setCounts(headcounts);
    setMembers(roster);
    setLoadState("loaded");
  }, [id, membership]);

  useEffect(() => {
    // Session-status gating (matches members.tsx): don't fetch - and don't fall through
    // to "Event not found" - while the session/chapter are still resolving.
    if (sessionStatus === "loading" || (membership !== null && chapterLoading)) return;
    // Fail soft: an errored fetch never leaves the screen hanging.
    //
    // c312: it used to fail soft into a LIE. Every failure set event=null, and null
    // renders "This event may have been removed, or it may not be shared with you" - so
    // a dropped connection told a legitimately invited guest they had been excluded.
    // That is the c299 class at its worst: not merely unhelpful, but affirmatively
    // wrong about the one thing the reader cares about.
    //
    // 404/403 ARE that message - gone, or genuinely not yours to see. Anything else is
    // our problem, not theirs, and says so with a retry.
    load().catch((error: unknown) => {
      setEvent(null);
      const denied = error instanceof ApiError && (error.status === 404 || error.status === 403);
      setLoadState(denied ? "loaded" : "error");
    });
  }, [load, sessionStatus, membership, chapterLoading]);

  const handleRsvp = async (status: RsvpStatus) => {
    if (!event) return;
    try {
      await setRsvp(event.id, status);
      await load();
    } catch (error) {
      showApiError(error, "Couldn't save your answer");
    }
  };

  const handleInvite = async (userIds: string[]) => {
    if (!event || userIds.length === 0) return;
    try {
      const updated = await inviteToEvent(event.id, userIds);
      setInvites(updated);
      setInviting(false);
    } catch (error) {
      showApiError(error, "Couldn't send those invites");
    }
  };

  const handleEdit = async (input: CreateEventInput) => {
    if (!event) return;
    try {
      // c202: ends_at and description go through AS-IS, null included. The sheet
      // always resolves both to a concrete value (a string, or null when the field
      // reads empty) rather than leaving them unset, so `?? undefined` here used to
      // turn "the host cleared this" into "omitted" - JSON.stringify drops undefined
      // keys (api/client.ts doFetch), which the backend reads as "leave it alone",
      // so a cleared end time or description silently kept its old value. Sending the
      // null through lets the backend's exclude_unset check see it and clear the
      // column instead.
      await updateEvent(event.id, {
        title: input.title,
        starts_at: input.starts_at,
        ends_at: input.ends_at,
        location: input.location,
        description: input.description,
        visibility: input.visibility,
      });
      setEditing(false);
      await load();
    } catch (error) {
      showApiError(error, "Couldn't save your changes");
    }
  };

  const handleCancel = () => {
    if (!event) return;
    confirmAction({
      title: "Call off this event?",
      message:
        "Everyone who RSVPd will see it marked as canceled. This cannot be undone - you would have to post a new event.",
      confirmLabel: "Call it off",
      destructive: true,
      onConfirm: () => {
        void (async () => {
          try {
            await cancelEvent(event.id);
            await load();
          } catch (error) {
            showApiError(error, "Couldn't cancel the event");
          }
        })();
      },
    });
  };

  // c312: this used to be a bare empty View - a blank screen with no indicator, which
  // reads as a broken app rather than as a wait (the c298 shape).
  if (loadState === "loading" || event === undefined) {
    return (
      <View
        style={{
          flex: 1,
          backgroundColor: palette.bg,
          paddingTop: insets.top + spacing.xl,
          paddingHorizontal: spacing.gutter,
        }}
      >
        <EmptyState title="Loading this event..." />
      </View>
    );
  }

  if (loadState === "error") {
    return (
      <View
        style={{
          flex: 1,
          backgroundColor: palette.bg,
          paddingTop: insets.top + spacing.xl,
          paddingHorizontal: spacing.gutter,
        }}
      >
        <EmptyState
          title="Couldn't load this event"
          message="Something went wrong reaching the server."
          actionLabel="Try again"
          onAction={() => {
            setLoadState("loading");
            void load().catch((error: unknown) => {
              setEvent(null);
              const denied =
                error instanceof ApiError && (error.status === 404 || error.status === 403);
              setLoadState(denied ? "loaded" : "error");
            });
          }}
        />
      </View>
    );
  }

  if (event === null) {
    return (
      <View
        style={{
          flex: 1,
          backgroundColor: palette.bg,
          paddingTop: insets.top + spacing.xl,
          paddingHorizontal: spacing.gutter,
        }}
      >
        <EmptyState
          title="Event not found"
          message="This event may have been removed, or it may not be shared with you."
        />
      </View>
    );
  }

  const isMember = membership?.chapter_id === event.chapter_id;
  const canManage =
    isMember &&
    event.canceled_at === null &&
    (user?.id === event.host_id || EBOARD_ROLES.includes(membership?.role ?? ""));
  const host = findMember(members, event.host_id);
  const myStatus = user ? (rsvps.find((rsvp) => rsvp.user_id === user.id)?.status ?? null) : null;
  const canceled = event.canceled_at !== null;

  // Invited, but has not answered. Its own group rather than folded into "Can't go":
  // silence is not a no, and a host reading the list needs to see who to chase.
  const answered = new Set(rsvps.map((rsvp) => rsvp.user_id));
  const awaiting = invites.filter((invite) => !answered.has(invite.invited_user_id));

  return (
    <View style={{ flex: 1, backgroundColor: palette.bg }}>
      <ScrollView
        showsVerticalScrollIndicator={false}
        contentContainerStyle={{ paddingBottom: spacing.xxxl }}
      >
        <View style={{ height: COVER_HEIGHT }}>
          <Image
            source={{ uri: event.cover_url }}
            style={{ width: "100%", height: "100%", opacity: canceled ? 0.45 : 1 }}
            resizeMode="cover"
          />
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
          {/* The cancellation notice sits ABOVE the title, because it changes what every
              line under it means. */}
          {canceled ? (
            <View
              style={{
                backgroundColor: palette.dangerSoft,
                borderRadius: radii.card,
                padding: spacing.lg,
                gap: spacing.xs,
              }}
            >
              <AppText variant="bodyBold" tone="danger">
                This event was called off
              </AppText>
              <AppText variant="caption" tone="secondary">
                The host cancelled it. Nobody else can RSVP.
              </AppText>
            </View>
          ) : null}

          <View style={{ gap: spacing.sm }}>
            <AppText variant="display">{event.title}</AppText>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <Feather name="calendar" size={16} color={palette.inkFaint} />
              <AppText variant="body" tone="secondary">
                {eventWhen(event.starts_at, event.ends_at)}
              </AppText>
            </View>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <Feather name="map-pin" size={16} color={palette.inkFaint} />
              <AppText variant="body" tone="secondary">
                {event.location}
              </AppText>
            </View>
            {isMember ? (
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
                <Feather name="eye" size={16} color={palette.inkFaint} />
                <AppText variant="body" tone="secondary">
                  {VISIBILITY_LABELS[event.visibility]}
                </AppText>
              </View>
            ) : null}
            <View
              style={{
                flexDirection: "row",
                alignItems: "center",
                gap: spacing.sm,
                marginTop: spacing.xs,
              }}
            >
              <GradientAvatar
                name={host?.display_name ?? "Host"}
                size={28}
                photoUrl={host?.avatar_url}
              />
              <AppText variant="caption" tone="secondary">
                Hosted by {host?.display_name ?? "your chapter"}
              </AppText>
            </View>
          </View>

          {event.description ? (
            <AppText variant="body" tone="secondary">
              {event.description}
            </AppText>
          ) : null}

          {!canceled ? (
            <View style={{ gap: spacing.sm }}>
              <SectionHeader title="Are you going?" />
              <View style={{ flexDirection: "row", gap: spacing.sm }}>
                {RSVP_OPTIONS.map((option) => {
                  const selected = myStatus === option.key;
                  // True headcount from the counts endpoint (c275); list-derived
                  // only when that call failed soft.
                  const count =
                    counts?.[option.key] ??
                    rsvps.filter((rsvp) => rsvp.status === option.key).length;
                  // Going count gets the "one gold moment" per §10 rule 4 - the org's
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
          ) : null}

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
                      const guest = findMember(members, rsvp.user_id);
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

            {awaiting.length > 0 ? (
              <View>
                <SectionHeader
                  title="Invited"
                  caption={`${awaiting.length} ${awaiting.length === 1 ? "person hasn't" : "people haven't"} answered`}
                />
                <Card>
                  {awaiting.map((invite, index) => {
                    const guest = findMember(members, invite.invited_user_id);
                    return (
                      <ListRow
                        key={invite.invited_user_id}
                        title={guest?.display_name ?? "Guest"}
                        left={
                          <GradientAvatar
                            name={guest?.display_name ?? "Guest"}
                            size={40}
                            photoUrl={guest?.avatar_url}
                          />
                        }
                        right={<Chip label="No reply" variant="neutral" />}
                        divider={index < awaiting.length - 1}
                      />
                    );
                  })}
                </Card>
              </View>
            ) : null}
          </View>

          {canManage ? (
            <View style={{ gap: spacing.sm }}>
              <Button label="Invite" variant="secondary" onPress={() => setInviting(true)} />
              <Button label="Edit event" variant="secondary" onPress={() => setEditing(true)} />
              <Button label="Call it off" variant="ghost" onPress={handleCancel} />
            </View>
          ) : null}
        </View>
      </ScrollView>

      <InviteSheet
        visible={inviting}
        members={members}
        alreadyInvited={new Set(invites.map((invite) => invite.invited_user_id))}
        hostId={event.host_id}
        onClose={() => setInviting(false)}
        onInvite={handleInvite}
      />

      {editing ? (
        <CreateEventSheet
          visible={editing}
          heading="Edit event"
          submitLabel="Save changes"
          initial={{
            title: event.title,
            starts_at: event.starts_at,
            ends_at: event.ends_at,
            location: event.location,
            cover_url: event.cover_url,
            description: event.description,
            visibility: event.visibility,
          }}
          onClose={() => setEditing(false)}
          onCreate={(input) => void handleEdit(input)}
        />
      ) : null}
    </View>
  );
}

interface InviteSheetProps {
  visible: boolean;
  members: MemberOut[];
  alreadyInvited: Set<string>;
  hostId: string;
  onClose: () => void;
  onInvite: (userIds: string[]) => void;
}

/**
 * Pick people off the chapter roster and invite them.
 *
 * THE ROSTER IS THE ONLY POOL, and that is a real limit rather than an oversight: there
 * is no user search endpoint, and inviting somebody outside the chapter needs a way to
 * name them that does not exist yet. Everything under it - the invite grant, the
 * awaiting-reply group, /me/event-invites - already works for any user id, so widening
 * the pool later is a change to this component and a search route, not to the model.
 *
 * Already-invited members stay listed and disabled rather than disappearing: a host
 * looking for someone they invited last week should find them, not wonder whether the
 * list is broken.
 */
function InviteSheet({
  visible,
  members,
  alreadyInvited,
  hostId,
  onClose,
  onInvite,
}: InviteSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const toggle = (userId: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  };

  const close = () => {
    setPicked(new Set());
    onClose();
  };

  // The host is already at their own party; offering to invite them is a no-op button.
  const invitable = members.filter((member) => member.user_id !== hostId);

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={close}>
      <Pressable
        onPress={close}
        style={{ flex: 1, backgroundColor: withAlpha(light.ink, 0.4), justifyContent: "flex-end" }}
      >
        <Pressable
          style={{
            backgroundColor: palette.surface,
            borderTopLeftRadius: radii.card,
            borderTopRightRadius: radii.card,
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.lg,
            paddingBottom: insets.bottom + spacing.lg,
            maxHeight: "80%",
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
          <AppText variant="title">Invite people</AppText>

          {invitable.length === 0 ? (
            <EmptyState
              title="Nobody to invite yet"
              message="Once your chapter has more members on the roster, they'll show up here."
            />
          ) : (
            <ScrollView showsVerticalScrollIndicator={false}>
              <Card>
                {invitable.map((member, index) => {
                  const already = alreadyInvited.has(member.user_id);
                  const selected = picked.has(member.user_id);
                  return (
                    <Pressable
                      key={member.user_id}
                      accessibilityRole="button"
                      accessibilityState={{ selected, disabled: already }}
                      disabled={already}
                      onPress={() => toggle(member.user_id)}
                      style={{ opacity: already ? 0.45 : 1 }}
                    >
                      <ListRow
                        title={member.display_name}
                        left={
                          <GradientAvatar
                            name={member.display_name}
                            size={40}
                            photoUrl={member.avatar_url}
                          />
                        }
                        right={
                          already ? (
                            <Chip label="Invited" variant="neutral" />
                          ) : (
                            <Feather
                              name={selected ? "check-circle" : "circle"}
                              size={20}
                              color={selected ? palette.accent : palette.inkFaint}
                            />
                          )
                        }
                        divider={index < invitable.length - 1}
                      />
                    </Pressable>
                  );
                })}
              </Card>
            </ScrollView>
          )}

          <Button
            label={picked.size === 0 ? "Select people to invite" : `Invite ${picked.size}`}
            onPress={() => {
              onInvite([...picked]);
              setPicked(new Set());
            }}
            disabled={picked.size === 0}
          />
        </Pressable>
      </Pressable>
    </Modal>
  );
}
