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
import { Image, Pressable, View, type ViewStyle } from "react-native";

import {
  createInvite,
  type ChapterInviteOut,
  type ChapterOut,
  type MembershipOut,
  type RoleName,
} from "@/api/chapters";
import { createEvent, listEventsWithRsvps, type EventOut, type EventRsvpOut, type EventWithRsvpsOut } from "@/api/events";
import { likePost, listPosts, unlikePost, type FeedPostOut } from "@/api/feed";
import { useSession, withInviteCode } from "@/auth";
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
// MOCK_CAMPUS/mockUserById remain: campus name display and the Events segment's
// host/RSVP lookups are outside this task's scope (cosmetic/pre-existing, per c44 split).
import { MOCK_CAMPUS, mockUserById } from "@/mocks/data";
import { cardShadow, radii, spacing, typography, useAppearance, useTheme } from "@/theme";

type FeatherIconName = ComponentProps<typeof Feather>["name"];
type OrgSegment = "feed" | "events" | "tools";

interface Tool {
  href: Href;
  icon: FeatherIconName;
  title: string;
  description: string;
  /** undefined = visible to every member. */
  roles?: RoleName[];
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
    href: "/chapter/treasurer",
    icon: "dollar-sign",
    title: "Treasurer",
    description: "Dues and the ledger",
    roles: ["treasurer", "president"],
  },
  {
    href: "/chapter/secretary",
    icon: "file-text",
    title: "Secretary",
    description: "Minutes and attendance",
    roles: ["secretary", "president"],
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

/** Compact relative age for card captions ("just now", "5m", "3h", "2d") — matches Home's feed. */
function age(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
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
 */
function OrgFeedSegment({ chapterId, orgName }: { chapterId: string; orgName: string }) {
  const [items, setItems] = useState<OrgFeedItem[] | null>(null);

  useEffect(() => {
    // ONE round trip: GET /chapters/{id}/posts returns FeedPostOut, which already
    // carries the author's display identity and batched like/comment counts (c43).
    // The old shape here fetched listLikes + listComments PER POST (2N queries)
    // and resolved authors through a separate roster call.
    // Fail soft: a failed fetch must not crash the feed segment.
    listPosts(chapterId)
      .then((posts) =>
        setItems(
          posts.map((post) => ({
            post,
            likeCount: post.like_count,
            likedByMe: post.liked_by_me,
          })),
        ),
      )
      .catch(() => setItems([]));
  }, [chapterId]);

  const toggleLike = async (item: OrgFeedItem) => {
    if (item.likedByMe) {
      await unlikePost(item.post.id);
    } else {
      await likePost(item.post.id);
    }
    setItems((current) =>
      (current ?? []).map((entry) =>
        entry.post.id === item.post.id
          ? {
              ...entry,
              likedByMe: !entry.likedByMe,
              likeCount: entry.likeCount + (entry.likedByMe ? -1 : 1),
            }
          : entry,
      ),
    );
  };

  if (items !== null && items.length === 0) {
    return (
      <EmptyState
        title="Nothing posted yet"
        message={`Chapter-only posts land here — only ${orgName} members ever see this feed.`}
      />
    );
  }

  return (
    <View style={{ gap: spacing.md }}>
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
        />
      ))}
    </View>
  );
}

/** Event card (§8.7 "the Partiful corner"): cover, title, date Chip, location, host row, RSVP stack. */
function EventCard({
  event,
  rsvps,
  onPress,
}: {
  event: EventOut;
  rsvps: EventRsvpOut[];
  onPress: () => void;
}) {
  const palette = useTheme();
  const host = mockUserById(event.host_id);
  const going = rsvps.filter((rsvp) => rsvp.status === "going");
  const goingPeople = going.map((rsvp) => {
    const user = mockUserById(rsvp.user_id);
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

  const reload = useCallback(async () => {
    // One round trip (c43) — the old shape here was listEvents + listRsvps per event.
    setEvents(await listEventsWithRsvps(chapterId));
  }, [chapterId]);

  useEffect(() => {
    // Fail soft: mock ids 422 against the live API until wiring lands.
    reload().catch(() => setEvents([]));
  }, [reload]);

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
 * the code and shows it prominently with the deep-link share text.
 * expo-clipboard isn't a project dependency yet, so the code/link render as
 * selectable text instead of adding a copy button + new dependency.
 */
function InviteCard({ chapterId, options }: { chapterId: string; options: RoleName[] }) {
  const palette = useTheme();
  const [inviteRole, setInviteRole] = useState<RoleName>(options[0] ?? "member");
  const [creating, setCreating] = useState(false);
  const [invite, setInvite] = useState<ChapterInviteOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setCreating(true);
    setError(null);
    try {
      const created = await createInvite(chapterId, { role: inviteRole });
      setInvite(created);
    } catch {
      setError("Couldn't create the invite. Try again.");
    } finally {
      setCreating(false);
    }
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
              Invite code · tap and hold to copy
            </AppText>
            <AppText variant="stat" selectable>
              {invite.code}
            </AppText>
            <AppText variant="caption" tone="tertiary" selectable>
              {withInviteCode("chirp://join-chapter", invite.code)}
            </AppText>
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
  const visible = TOOLS.filter((tool) => tool.roles === undefined || tool.roles.includes(role));
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
}: {
  membership: MembershipOut;
  chapter: ChapterOut | null;
  segment: OrgSegment;
  onSegmentChange: (segment: OrgSegment) => void;
}) {
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
            {chapter.chapter_name !== null ? `${chapter.chapter_name} · ${MOCK_CAMPUS.name}` : MOCK_CAMPUS.name}
          </AppText>
          <Chip label={ROLE_LABELS[role]} variant="accent" style={{ marginTop: spacing.xs }} />
        </View>
      </HeroCard>

      <OrgSegmentedControl segment={segment} onChange={onSegmentChange} />

      {segment === "feed" ? <OrgFeedSegment chapterId={chapter.id} orgName={chapter.org_name} /> : null}
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
  const [segment, setSegment] = useState<OrgSegment>("feed");

  // Session-status gating (PR #6 review): a real member must never flash the
  // non-member "No orgs yet" state on cold start — only render FindYourOrg
  // once the session has actually settled AND resolved to no membership.
  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading);

  return (
    <View style={{ flex: 1 }}>
      <Screen
        title="Orgs"
        eyebrow={`${MOCK_CAMPUS.name.toUpperCase()} · SPARTANS`}
        accentBarColor={campusColors.secondary}
        subtitle={
          loading
            ? undefined
            : membership !== null
              ? "Your chapter, your tools."
              : `Find your org at ${MOCK_CAMPUS.name}`
        }
      >
        {loading ? (
          <EmptyState title="Loading your org..." />
        ) : membership !== null ? (
          <MemberOrgHub membership={membership} chapter={chapter} segment={segment} onSegmentChange={setSegment} />
        ) : (
          <FindYourOrg />
        )}
      </Screen>
      {/* Org-colored composer FAB (§8.7) — Feed segment only, mirrors Home's Fab pattern. */}
      {!loading && membership !== null && segment === "feed" ? <Fab /> : null}
    </View>
  );
}
