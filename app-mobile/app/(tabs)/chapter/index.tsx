/**
 * Orgs tab per DESIGN §6/§8.7 — two states:
 * member: HeroCard org identity + role Chip, then a pill segmented control
 *   (Feed · Events · Tools) under the hero — the org's own private world,
 *   entirely in its own colors via OrgAccentScope (wrapped once at the
 *   chapter/_layout.tsx Stack level, so every screen/component below
 *   re-accents automatically).
 * non-member: sessionStatus is "ready" and useOwnChapter().membership is
 *   null — "No orgs yet" EmptyState routing to /join-chapter, plus a
 *   browsable category section (greek registration stays opt-in per §6).
 *   Gated on sessionStatus (PR #6 review) so a real member never flashes
 *   this state while the session is still loading on cold start. Route dir
 *   stays `chapter/` for backend parity.
 */

import { useRouter, type Href } from "expo-router";
import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useCallback, useEffect, useState } from "react";
import { Alert, Image, Pressable, Share, View, type ViewStyle } from "react-native";
import QRCode from "react-native-qrcode-svg";

import {
  createInvite,
  listInvites,
  listMembers,
  revokeInvite,
  type Capability,
  type ChapterInviteOut,
  type ChapterOut,
  type MemberOut,
  type MembershipOut,
  type RoleName,
} from "@/api/chapters";
import { ApiError } from "@/api/client";
import { createEvent, listEventsWithRsvps, type EventOut, type EventRsvpOut, type EventWithRsvpsOut } from "@/api/events";
import { likePost, listPosts, unlikePost, type FeedPostOut } from "@/api/feed";
import { blockUser, createReport } from "@/api/moderation";
import { inviteShareUrl, useCampus, useSession } from "@/auth";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import {
  AppText,
  AvatarStack,
  Button,
  Card,
  Chip,
  CreateEventSheet,
  EmptyState,
  Fab,
  GradientAvatar,
  HeroCard,
  MediaPostCard,
  Screen,
  SectionHeader,
  type CreateEventInput,
} from "@/components";
import { cardShadow, radii, spacing, typography, useAppearance, useTheme } from "@/theme";

type FeatherIconName = ComponentProps<typeof Feather>["name"];
type OrgSegment = "feed" | "events" | "tools";

interface Tool {
  href: Href;
  icon: FeatherIconName;
  title: string;
  description: string;
  /**
   * undefined = visible to every member. Otherwise the SERVER-NAMED capability the
   * caller must hold (c80). Never a role list: the server decides who holds a
   * capability, and a client-side role list is a second copy of permissions.py that
   * drifts silently — nothing fails when it does, you just get a tile nobody can use
   * or an officer who never sees their own dashboard.
   */
  capability?: Capability;
}

const TOOLS: Tool[] = [
  { href: "/chapter/tree", icon: "git-branch", title: "Family Tree", description: "Bigs, littles, and lineage" },
  { href: "/chapter/members", icon: "users", title: "Members", description: "The full roster, by role" },
  {
    href: "/chapter/alumni",
    icon: "briefcase",
    title: "Alumni",
    description: "Directory, contacts, and job board",
  },
  {
    href: "/chapter/dues",
    icon: "credit-card",
    title: "Dues",
    description: "What you owe, and how to pay it",
  },
  {
    href: "/chapter/historian",
    icon: "book-open",
    title: "Historian",
    description: "Families, bigs, and littles",
    capability: "lineage_admin",
  },
  {
    href: "/chapter/treasurer",
    icon: "dollar-sign",
    title: "Treasurer",
    description: "Dues and the ledger",
    capability: "dues_admin",
  },
  {
    href: "/chapter/secretary",
    icon: "file-text",
    title: "Secretary",
    description: "Minutes and attendance",
    capability: "minutes_admin",
  },
  {
    href: "/chapter/president",
    icon: "award",
    title: "President",
    description: "Roles, status, and chapter details",
    capability: "members_admin",
  },
  {
    href: "/chapter/vice-president",
    icon: "eye",
    title: "Deputy President",
    description: "Read-only roster, invites, and dues",
    capability: "deputy_overview",
  },
];

const ROLE_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Member",
  pledge: "Pledge",
  alumni: "Alum",
};

/** Runtime fallback for a role the closed ROLE_LABELS record doesn't know yet —
 * the server owns the taxonomy (c44), so an unmapped value just gets prettified. */
function roleLabel(role: RoleName): string {
  return ROLE_LABELS[role] ?? role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const CATEGORIES = ["Fraternities", "Sororities", "Clubs", "Intramurals"] as const;
type Category = (typeof CATEGORIES)[number];

const SEGMENTS: { key: OrgSegment; label: string }[] = [
  { key: "feed", label: "Feed" },
  { key: "events", label: "Events" },
  { key: "tools", label: "Tools" },
];

/** Resolve a user id against the chapter roster — the only name source available
 * (there is no GET /users/{id}). Mirrors the helper in chapter/event/[id].tsx. */
function findMember(members: MemberOut[], userId: string): MemberOut | undefined {
  return members.find((member) => member.user_id === userId);
}

/** Compact relative age for card captions ("just now", "5m", "3h", "2d") — matches Home's feed. */
function age(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

/** Pill segmented control under the org hero (§8.7): Feed · Events · Tools, org-accent active state. */
function OrgSegmentedControl({
  segment,
  onChange,
}: {
  segment: OrgSegment;
  onChange: (segment: OrgSegment) => void;
}) {
  const palette = useTheme();
  return (
    <View style={{ flexDirection: "row", gap: spacing.sm }}>
      {SEGMENTS.map((option) => {
        const active = option.key === segment;
        return (
          <Pressable
            key={option.key}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
            onPress={() => onChange(option.key)}
            style={({ pressed }) => ({
              flex: 1,
              alignItems: "center",
              paddingVertical: spacing.sm,
              borderRadius: radii.pill,
              backgroundColor: active ? palette.accent : palette.surfaceAlt,
              opacity: pressed ? 0.85 : 1,
            })}
          >
            <AppText variant="bodyBold" tone={active ? "onAccent" : "secondary"}>
              {option.label}
            </AppText>
          </Pressable>
        );
      })}
    </View>
  );
}

/** Local optimistic overlay on top of the server's batched counts — the server
 * owns like_count/liked_by_me, this only carries the un-committed tap. */
interface OrgFeedItem {
  post: FeedPostOut;
  likeCount: number;
  likedByMe: boolean;
}

/**
 * Feed segment (§8.7): chapter-only posts, never on the FYP. GET
 * /chapters/{id}/posts already scopes to this chapter, newest first, and
 * excludes soft-deleted rows server-side (backend/app/routers/feed.py), so
 * no client-side chapter/source filtering belongs here.
 *
 * Moderation (board c35): MediaPostCard owns the report/block overflow menu
 * UI; this segment owns the actual API calls and the post-block cleanup —
 * dropping the blocked author's posts locally, then refetching, since GET
 * /chapters/{chapter_id}/posts now filters blocked authors server-side.
 */
function OrgFeedSegment({
  chapterId,
  orgName,
  refreshKey,
}: {
  chapterId: string;
  orgName: string;
  refreshKey: number;
}) {
  const { user } = useSession();
  const [items, setItems] = useState<OrgFeedItem[] | null>(null);
  // Honest signal (board c102): true only for a non-active viewer when this
  // chapter genuinely has actives-only content they cannot see. Never true for an
  // active member, who already sees everything.
  const [activesOnlyHidden, setActivesOnlyHidden] = useState(false);

  // ONE round trip: GET /chapters/{id}/posts returns FeedPostOut, which already
  // carries the author's display identity and batched like/comment counts (c43).
  // The old shape here fetched a per-post likes and comments call (2N queries)
  // and resolved authors through a separate roster call.
  // Fail soft (internally, not via a thrown rejection): a failed fetch must not
  // crash the feed segment, and this needs to be safely re-awaitable from
  // blockAuthor's refetch below without that refetch masquerading as a block failure.
  const load = useCallback(async () => {
    try {
      const { posts, activesOnlyHidden: hidden } = await listPosts(chapterId);
      setActivesOnlyHidden(hidden);
      setItems(
        posts.map((post) => ({
          post,
          likeCount: post.like_count,
          likedByMe: post.liked_by_me,
        })),
      );
    } catch {
      setItems([]);
    }
  }, [chapterId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const reportPost = async (item: OrgFeedItem, reason: string) => {
    try {
      await createReport({ target_type: "post", target_id: item.post.id, reason });
      Alert.alert("Reported", "Thanks for letting us know.");
    } catch (error) {
      showApiError(error, "Couldn't send that report");
    }
  };

  const blockAuthor = async (item: OrgFeedItem) => {
    try {
      await blockUser(item.post.author_id);
    } catch (error) {
      showApiError(error, "Couldn't block that person");
      return;
    }
    // Drop every post by this author immediately so the UI reacts at once...
    setItems((current) =>
      (current ?? []).filter((entry) => entry.post.author_id !== item.post.author_id),
    );
    // ...then refetch, since GET /chapters/{chapter_id}/posts now filters this
    // author's posts server-side (c35). `load` fails soft internally, so this
    // can't turn a successful block into a spurious error alert.
    await load();
  };

  const toggleLike = async (item: OrgFeedItem) => {
    // c120: mirrors feed/index.tsx's toggleLike. That one flips optimistically
    // and rolls back on failure; this one used to await the network call FIRST
    // with no catch at all, so a failed request became an unhandled rejection
    // instead of the UI just staying as it was.
    const wasLiked = item.likedByMe;
    setItems((current) =>
      (current ?? []).map((entry) =>
        entry.post.id === item.post.id
          ? { ...entry, likedByMe: !wasLiked, likeCount: entry.likeCount + (wasLiked ? -1 : 1) }
          : entry,
      ),
    );
    try {
      if (wasLiked) {
        await unlikePost(item.post.id);
      } else {
        await likePost(item.post.id);
      }
    } catch {
      setItems((current) =>
        (current ?? []).map((entry) =>
          entry.post.id === item.post.id
            ? { ...entry, likedByMe: wasLiked, likeCount: entry.likeCount + (wasLiked ? 1 : -1) }
            : entry,
        ),
      );
    }
  };

  // Shown ABOVE both the empty state and the list (board c102's named failure
  // mode): a non-active viewer who sees zero posts must still be able to tell
  // that's because the chapter-public tier is genuinely empty, not because an
  // actives-only tier exists and is simply invisible to them. Gating this behind
  // items.length > 0 would silently recreate exactly that ambiguity.
  const hiddenNotice = activesOnlyHidden ? <ActivesOnlyHiddenNotice /> : null;

  if (items !== null && items.length === 0) {
    return (
      <View style={{ gap: spacing.md }}>
        {hiddenNotice}
        <EmptyState
          title="Nothing posted yet"
          message={`Chapter-only posts land here — only ${orgName} members ever see this feed.`}
        />
      </View>
    );
  }

  return (
    <View style={{ gap: spacing.md }}>
      {hiddenNotice}
      {(items ?? []).map((item) => (
        <MediaPostCard
          key={item.post.id}
          post={item.post}
          authorName={item.post.display_name}
          authorPhotoUrl={item.post.avatar_url}
          timeLabel={age(item.post.created_at)}
          likeCount={item.likeCount}
          commentCount={item.post.comment_count}
          likedByMe={item.likedByMe}
          onToggleLike={() => void toggleLike(item)}
          onReport={(reason) => void reportPost(item, reason)}
          onBlock={() => void blockAuthor(item)}
          canBlock={user !== null && item.post.author_id !== user.id}
        />
      ))}
    </View>
  );
}

/** Honest-signal row (board c102's named failure mode): tells a non-active member
 * a fuller, actives-only tier exists in this chapter, rather than leaving them
 * reading an indistinguishable-from-quiet feed. Modest — one row, no dismiss,
 * matches the "stated, not offered" info box CreateSheet uses for its own
 * audience-picker constraint. */
function ActivesOnlyHiddenNotice() {
  const palette = useTheme();
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.md,
        padding: spacing.md,
        borderRadius: radii.input,
        backgroundColor: palette.surfaceAlt,
        borderWidth: 1,
        borderColor: palette.border,
      }}
    >
      <Feather name="eye-off" size={18} color={palette.inkFaint} />
      <View style={{ flex: 1, gap: 2 }}>
        <AppText variant="bodyBold">Actives-only posts are hidden</AppText>
        <AppText variant="caption" tone="secondary">
          Some posts here are visible only to active members.
        </AppText>
      </View>
    </View>
  );
}

/** Event card (§8.7 "the Partiful corner"): cover, title, date Chip, location, host row, RSVP stack. */
function EventCard({
  event,
  rsvps,
  members,
  onPress,
}: {
  event: EventOut;
  rsvps: EventRsvpOut[];
  members: MemberOut[];
  onPress: () => void;
}) {
  const palette = useTheme();
  const host = findMember(members, event.host_id);
  const going = rsvps.filter((rsvp) => rsvp.status === "going");
  const goingPeople = going.map((rsvp) => {
    const user = findMember(members, rsvp.user_id);
    return { name: user?.display_name ?? "Guest", photoUrl: user?.avatar_url };
  });

  const cardBase: ViewStyle = {
    backgroundColor: palette.surface,
    borderRadius: radii.card,
    borderWidth: 1,
    borderColor: palette.border,
    overflow: "hidden",
    ...cardShadow(palette),
  };

  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [cardBase, { opacity: pressed ? 0.92 : 1 }]}
    >
      <View style={{ height: 160 }}>
        <Image source={{ uri: event.cover_url }} style={{ width: "100%", height: "100%" }} resizeMode="cover" />
        <Chip
          label={event.date_label}
          variant="accent"
          style={{ position: "absolute", top: spacing.md, left: spacing.md }}
        />
      </View>
      <View style={{ padding: spacing.lg, gap: spacing.sm }}>
        <AppText variant="title">{event.title}</AppText>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs }}>
          <Feather name="map-pin" size={13} color={palette.inkFaint} />
          <AppText variant="caption" tone="secondary" numberOfLines={1}>
            {event.location}
          </AppText>
        </View>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
          <GradientAvatar name={host?.display_name ?? "Host"} size={22} photoUrl={host?.avatar_url} />
          <AppText variant="caption" tone="secondary">
            Hosted by {host?.display_name ?? "Unknown"}
          </AppText>
        </View>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm, marginTop: spacing.xs }}>
          <AvatarStack people={goingPeople} size={24} />
          <AppText variant="bodyBold">{going.length} going</AppText>
        </View>
      </View>
    </Pressable>
  );
}

/** Events segment (§8.7): event list + mock create-event sheet, wired to src/api/events.ts. */
function OrgEventsSegment({ chapterId }: { chapterId: string }) {
  const router = useRouter();
  const palette = useTheme();
  const [events, setEvents] = useState<EventWithRsvpsOut[] | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  // Fetched once here, not per card: the roster is the only way to turn a
  // host_id/rsvp.user_id into a name (no GET /users/{id} exists).
  const [members, setMembers] = useState<MemberOut[]>([]);

  const reload = useCallback(async () => {
    // One round trip (c43) — the old shape here was listEvents + listRsvps per event.
    setEvents(await listEventsWithRsvps(chapterId));
  }, [chapterId]);

  useEffect(() => {
    // Fail soft: a failed events load must not crash the segment.
    reload().catch(() => setEvents([]));
  }, [reload]);

  useEffect(() => {
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setMembers([]));
  }, [chapterId]);

  const handleCreate = async (input: CreateEventInput) => {
    await createEvent(chapterId, input);
    await reload();
  };

  return (
    <View style={{ gap: spacing.lg }}>
      <SectionHeader
        title="Upcoming"
        caption="The Partiful corner — plan the next one"
        right={
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="New event"
            onPress={() => setSheetOpen(true)}
            hitSlop={spacing.sm}
            style={({ pressed }) => ({
              width: 32,
              height: 32,
              borderRadius: radii.pill,
              backgroundColor: palette.accentSoft,
              alignItems: "center",
              justifyContent: "center",
              opacity: pressed ? 0.8 : 1,
            })}
          >
            <Feather name="plus" size={16} color={palette.accent} />
          </Pressable>
        }
      />

      {events !== null && events.length === 0 ? (
        <EmptyState
          title="No events yet"
          message="Plan a rush cookout, a formal, or a tailgate — it takes a minute."
          actionLabel="New event"
          onAction={() => setSheetOpen(true)}
        />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(events ?? []).map(({ event, rsvps }) => (
            <EventCard
              key={event.id}
              event={event}
              rsvps={rsvps}
              members={members}
              onPress={() => router.push(`/chapter/event/${event.id}`)}
            />
          ))}
        </View>
      )}

      <CreateEventSheet
        visible={sheetOpen}
        onClose={() => setSheetOpen(false)}
        onCreate={(input) => void handleCreate(input)}
      />
    </View>
  );
}

/**
 * E-board invite-create card (Tools segment, §8.7/§10 pill-card idiom):
 * role picker (Chip row) whose options come from GET /chapters/{id}/role-meta
 * (c44) — the server applies the create_invite rule (any e-board mints
 * member/pledge/alumni, president additionally mints e-board roles), so this
 * file no longer mirrors permissions.py. Then a "Create invite" Button mints
 * the code and shows it prominently with the public link, native share sheet,
 * and an on-device QR code. Universal-link association files remain an
 * external release setup task; the QR always encodes the same https hand-off
 * page so it is useful before those files exist.
 */
/** What a code is right now, in the order that decides it (c111).
 *
 * Revoked beats expired beats spent: a president who turned a code off should see
 * "Turned off" whatever else has since become true of it, because that is the fact
 * they acted on. Only a code that is none of these can still let someone in, and
 * only that one gets a revoke action — offering to turn off a dead code implies it
 * was doing something. */
function inviteStanding(invite: ChapterInviteOut): { deadReason: string | null } {
  if (invite.revoked_at !== null) return { deadReason: "Turned off" };
  if (new Date(invite.expires_at).getTime() <= Date.now()) return { deadReason: "Expired" };
  if (invite.uses >= invite.max_uses) return { deadReason: "Used up" };
  return { deadReason: null };
}

function InviteCard({ chapterId, options }: { chapterId: string; options: RoleName[] }) {
  const palette = useTheme();
  const [inviteRole, setInviteRole] = useState<RoleName>(options[0] ?? "member");
  const [creating, setCreating] = useState(false);
  const [invite, setInvite] = useState<ChapterInviteOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [existing, setExisting] = useState<ChapterInviteOut[] | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [showQr, setShowQr] = useState(false);
  const [sharing, setSharing] = useState(false);

  // c111: the codes already out there. Fails soft to absent rather than showing a
  // broken shell — the mint half of this card has to keep working if the list
  // call dies, since minting is what an e-board came here to do.
  const refreshExisting = useCallback(async () => {
    try {
      setExisting(await listInvites(chapterId));
    } catch {
      setExisting(null);
    }
  }, [chapterId]);

  useEffect(() => {
    void refreshExisting();
  }, [refreshExisting]);

  const create = async () => {
    setCreating(true);
    setError(null);
    try {
      const created = await createInvite(chapterId, { role: inviteRole });
      setInvite(created);
      setShowQr(false);
      void refreshExisting();
    } catch {
      setError("Couldn't create the invite. Try again.");
    } finally {
      setCreating(false);
    }
  };

  // Confirmed rather than instant: this is not undoable through any screen in the
  // app, and the whole point of the code is that other people are holding it.
  const confirmRevoke = (target: ChapterInviteOut) => {
    Alert.alert(
      "Turn off this code?",
      `${target.code} stops working immediately. Anyone still holding it will need a new one.`,
      [
        { text: "Keep it", style: "cancel" },
        {
          text: "Turn it off",
          style: "destructive",
          onPress: () => {
            void (async () => {
              setRevoking(target.code);
              try {
                await revokeInvite(chapterId, target.code);
                await refreshExisting();
              } catch {
                setError("Couldn't turn that code off. Try again.");
              } finally {
                setRevoking(null);
              }
            })();
          },
        },
      ],
    );
  };

  return (
    <Card>
      <View style={{ gap: spacing.md }}>
        <View style={{ gap: spacing.xs }}>
          <AppText variant="headline">Invite someone</AppText>
          <AppText variant="caption" tone="secondary">
            E-board only — pick a role and mint a code.
          </AppText>
        </View>

        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
          {options.map((option) => (
            <Pressable
              key={option}
              accessibilityRole="button"
              accessibilityState={{ selected: inviteRole === option }}
              onPress={() => {
                setInviteRole(option);
                setInvite(null);
                setShowQr(false);
              }}
              style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
            >
              <Chip label={roleLabel(option)} variant={inviteRole === option ? "accent" : "neutral"} />
            </Pressable>
          ))}
        </View>

        {error !== null ? (
          <AppText variant="caption" tone="danger">
            {error}
          </AppText>
        ) : null}

        <Button
          label={creating ? "Creating..." : "Create invite"}
          onPress={() => void create()}
          disabled={creating}
        />

        {invite !== null ? (
          <View
            style={{
              gap: spacing.xs,
              padding: spacing.md,
              borderRadius: radii.input,
              backgroundColor: palette.surfaceAlt,
            }}
          >
            <AppText variant="caption" tone="secondary">
              Invite code · selectable or shareable
            </AppText>
            <AppText variant="stat" selectable>
              {invite.code}
            </AppText>
            {/* Shared as https, not chirp://: a custom scheme arrives as dead
                text in Messages and most DM apps, which is exactly where invites
                get sent. The web page bounces it back into the app. */}
            <AppText variant="caption" tone="tertiary" selectable>
              {inviteShareUrl(invite.code)}
            </AppText>
            <View style={{ gap: spacing.sm, marginTop: spacing.sm }}>
              <Button
                label={sharing ? "Opening share sheet..." : "Share invite"}
                variant="secondary"
                disabled={sharing}
                onPress={() => {
                  const url = inviteShareUrl(invite.code);
                  setSharing(true);
                  void Share.share({
                    title: "Join us on Chirp",
                    message: `Join our org on Chirp: ${url}`,
                    url,
                  }).catch(() => {
                    setError("Couldn't open the share sheet. You can still copy the link above.");
                  }).finally(() => setSharing(false));
                }}
              />
              <Button
                label={showQr ? "Hide QR code" : "Show QR code"}
                variant="ghost"
                onPress={() => setShowQr((visible) => !visible)}
              />
            </View>
            {showQr ? (
              <View
                accessible
                accessibilityLabel="QR code for this Chirp invite"
                style={{ alignItems: "center", gap: spacing.sm, paddingTop: spacing.sm }}
              >
                <QRCode
                  value={inviteShareUrl(invite.code)}
                  size={184}
                  color={palette.ink}
                  backgroundColor={palette.surface}
                  quietZone={spacing.sm}
                />
                <AppText variant="caption" tone="secondary" style={{ textAlign: "center" }}>
                  Scan this code to open the invite page.
                </AppText>
              </View>
            ) : null}
          </View>
        ) : null}

        {/* c111: codes already in circulation. Before this, revocation existed on
            the server and could only be reached for a code you were still looking
            at — which is never the one that leaked. */}
        {existing !== null && existing.length > 0 ? (
          <View style={{ gap: spacing.sm }}>
            <AppText variant="caption" tone="secondary">
              Codes you have made
            </AppText>
            {existing.map((row) => {
              const standing = inviteStanding(row);
              return (
                <View
                  key={row.id}
                  style={{
                    flexDirection: "row",
                    alignItems: "center",
                    gap: spacing.sm,
                    paddingVertical: spacing.xs,
                  }}
                >
                  <View style={{ flex: 1, gap: 2 }}>
                    <AppText variant="body" selectable>
                      {row.code}
                    </AppText>
                    <AppText variant="caption" tone="tertiary">
                      {`${roleLabel(row.role)} · ${row.uses} of ${row.max_uses} used`}
                    </AppText>
                  </View>
                  {standing.deadReason === null ? (
                    <Pressable
                      accessibilityRole="button"
                      accessibilityLabel={`Turn off invite code ${row.code}`}
                      disabled={revoking === row.code}
                      onPress={() => confirmRevoke(row)}
                      style={({ pressed }) => ({ opacity: pressed || revoking === row.code ? 0.5 : 1 })}
                    >
                      <Chip label={revoking === row.code ? "..." : "Turn off"} variant="neutral" />
                    </Pressable>
                  ) : (
                    <Chip label={standing.deadReason} variant="neutral" />
                  )}
                </View>
              );
            })}
          </View>
        ) : null}
      </View>
    </Card>
  );
}

/** Tools segment (§8.7): the pre-existing role-gated tool grid, unchanged, plus the e-board invite card. */
function OrgToolsSegment({ chapterId, role }: { chapterId: string; role: RoleName }) {
  const router = useRouter();
  const palette = useTheme();
  const { roleMeta } = useOwnChapter();
  // c80: every gated tile now asks the server what this caller may DO. While
  // roleMeta is loading or errored this is [], so every gated tile is ABSENT —
  // fails CLOSED rather than flashing open, matching the invite card's posture.
  const capabilities = roleMeta?.capabilities ?? [];
  const allTools: Tool[] = [
    ...TOOLS,
    {
      href: "/chapter/moderation",
      icon: "shield",
      title: "Moderation",
      description: "Open reports and yak removal",
      capability: "moderation",
    },
  ];
  const visible = allTools.filter(
    (tool) => tool.capability === undefined || capabilities.includes(tool.capability),
  );
  // Server-decided (c44): a non-empty invitable set means this caller may mint
  // invites. Fail soft — while roleMeta is loading (or errored) the card is
  // simply absent, never shown to someone the backend would 403.
  const invitable = roleMeta?.invitable ?? [];

  return (
    <View style={{ gap: spacing.md }}>
      {invitable.length > 0 ? <InviteCard chapterId={chapterId} options={invitable} /> : null}

      {/* First tool gets a featured full-width row (§10 rule 1 — vary card sizes,
          not an unbroken grid of identical tiles); the rest share a 2-col grid. */}
      {visible[0] !== undefined ? (
        <Card onPress={() => router.push(visible[0].href)}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
            <Feather name={visible[0].icon} size={typography.display.fontSize} color={palette.accent} />
            <View style={{ flex: 1, gap: spacing.xs }}>
              <AppText variant="headline">{visible[0].title}</AppText>
              <AppText variant="caption" tone="secondary">
                {visible[0].description}
              </AppText>
            </View>
            <Feather name="chevron-right" size={typography.title.fontSize} color={palette.inkFaint} />
          </View>
        </Card>
      ) : null}

      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.md }}>
        {visible.slice(1).map((tool) => (
          <Card
            key={tool.title}
            onPress={() => router.push(tool.href)}
            style={{ flexBasis: "47%", flexGrow: 1 }}
          >
            <View style={{ gap: spacing.sm }}>
              <Feather name={tool.icon} size={typography.title.fontSize} color={palette.accent} />
              <AppText variant="headline">{tool.title}</AppText>
              <AppText variant="caption" tone="secondary">
                {tool.description}
              </AppText>
            </View>
          </Card>
        ))}
      </View>
    </View>
  );
}

/**
 * Member state: org identity hero, then the Feed/Events/Tools segmented
 * control (§8.7). `chapter` comes from OwnChapterProvider (mounted in
 * chapter/_layout.tsx, single-org world — memberships[0]) — OrgsScreen only
 * renders this once chapter loading has settled, so null here means the
 * fetch actually failed.
 */
function MemberOrgHub({
  membership,
  chapter,
  segment,
  onSegmentChange,
  feedRefreshKey,
}: {
  membership: MembershipOut;
  chapter: ChapterOut | null;
  segment: OrgSegment;
  onSegmentChange: (segment: OrgSegment) => void;
  feedRefreshKey: number;
}) {
  // Called before the early return below: hooks cannot run conditionally.
  const campus = useCampus();

  if (chapter === null) {
    return <EmptyState title="Couldn't load your org" message="Check your connection and try again." />;
  }

  const role = membership.role;

  return (
    <View style={{ gap: spacing.xl }}>
      <HeroCard>
        <View style={{ gap: spacing.sm }}>
          <AppText variant="micro" tone="onAccent">
            Your org
          </AppText>
          <AppText variant="title" tone="onAccent">
            {chapter.org_name}
          </AppText>
          <AppText variant="caption" tone="onAccent">
            {/* Real campus name (GET /campuses/{id}, c46). Until it resolves the
                chapter name stands alone rather than being paired with a wrong
                campus — this used to be a hardcoded MOCK_CAMPUS. */}
            {[chapter.chapter_name, campus?.name].filter(Boolean).join(" · ")}
          </AppText>
          <Chip label={ROLE_LABELS[role]} variant="accent" style={{ marginTop: spacing.xs }} />
        </View>
      </HeroCard>

      <OrgSegmentedControl segment={segment} onChange={onSegmentChange} />

      {segment === "feed" ? (
        <OrgFeedSegment chapterId={chapter.id} orgName={chapter.org_name} refreshKey={feedRefreshKey} />
      ) : null}
      {segment === "events" ? <OrgEventsSegment chapterId={chapter.id} /> : null}
      {segment === "tools" ? <OrgToolsSegment chapterId={chapter.id} role={role} /> : null}
    </View>
  );
}

/** Non-member state per DESIGN §6: "No orgs yet" EmptyState routing to the dedicated
 * /join-chapter screen (which already owns code redemption + error handling), plus a
 * browsable category section — greek registration stays opt-in here. */
function FindYourOrg() {
  const router = useRouter();
  const [category, setCategory] = useState<Category>("Fraternities");

  return (
    <View style={{ gap: spacing.xl }}>
      <EmptyState
        title="No orgs yet"
        message="Join a fraternity, sorority, or campus org with an invite code from their e-board."
        actionLabel="Enter invite code"
        onAction={() => router.push("/join-chapter")}
      />

      <View>
        <SectionHeader title="Browse by category" caption="Every kind of org lives here" />
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
          {CATEGORIES.map((option) => (
            <Pressable
              key={option}
              accessibilityRole="button"
              accessibilityState={{ selected: category === option }}
              onPress={() => setCategory(option)}
              style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
            >
              <Chip label={option} variant={category === option ? "accent" : "neutral"} />
            </Pressable>
          ))}
        </View>
        <EmptyState
          title="Org discovery is coming"
          message={`Browsing ${category.toLowerCase()} lands soon — join with an invite code for now.`}
        />
      </View>
    </View>
  );
}

export default function OrgsScreen() {
  const { campusColors } = useAppearance();
  const { sessionStatus, membership, chapter, chapterLoading } = useOwnChapter();
  const campus = useCampus();
  const [segment, setSegment] = useState<OrgSegment>("feed");
  const [feedRefreshKey, setFeedRefreshKey] = useState(0);

  // Session-status gating (PR #6 review): a real member must never flash the
  // non-member "No orgs yet" state on cold start — only render FindYourOrg
  // once the session has actually settled AND resolved to no membership.
  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading);

  return (
    <View style={{ flex: 1 }}>
      <Screen
        title="Orgs"
        // Real campus name (c46), absent until it resolves — an absent eyebrow
        // beats a wrong one. Was MOCK_CAMPUS plus a hardcoded "· SPARTANS",
        // which is UNCG's mascot and wrong for every other campus; CampusOut has
        // no mascot field to replace it with.
        eyebrow={campus ? campus.name.toUpperCase() : undefined}
        accentBarColor={campusColors.secondary}
        subtitle={
          loading
            ? undefined
            : membership !== null
              ? "Your chapter, your tools."
              : campus
                ? `Find your org at ${campus.name}`
                : "Find your org"
        }
        // Fab below only renders on the feed segment once membership resolves
        // — matched exactly here so scroll content (including the Tools grid
        // on other segments) clears whichever overlays are actually showing
        // (c168).
        hasFab={!loading && membership !== null && segment === "feed"}
      >
        {loading ? (
          <EmptyState title="Loading your org..." />
        ) : membership !== null ? (
          <MemberOrgHub
            membership={membership}
            chapter={chapter}
            segment={segment}
            onSegmentChange={setSegment}
            feedRefreshKey={feedRefreshKey}
          />
        ) : (
          <FindYourOrg />
        )}
      </Screen>
      {/* Org-colored composer FAB (§8.7) — Feed segment only, mirrors Home's Fab pattern. */}
      {!loading && membership !== null && segment === "feed" ? (
        <Fab
          chapterId={membership.chapter_id}
          campusId={campus?.id ?? null}
          campusName={campus?.name ?? null}
          isActiveMember={membership.status === "active"}
          onPosted={() => setFeedRefreshKey((key) => key + 1)}
        />
      ) : null}
    </View>
  );
}
