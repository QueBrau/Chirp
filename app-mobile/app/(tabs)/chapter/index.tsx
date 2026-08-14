/**
 * Orgs tab per DESIGN §6/§8.7 — two states:
 * member: HeroCard org identity + role Chip, then a pill segmented control
 *   (Feed · Events · Tools) under the hero — the org's own private world,
 *   entirely in its own colors via OrgAccentScope (wrapped once at the
 *   chapter/_layout.tsx Stack level, so every screen/component below
 *   re-accents automatically).
 * non-member (behind mockIsOrgMember): "Find your org" — invite code input,
 *   category chips, EmptyState. Route dir stays `chapter/` for backend parity.
 */

import { useRouter, type Href } from "expo-router";
import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useCallback, useEffect, useState } from "react";
import { Image, Pressable, TextInput, View, type ViewStyle } from "react-native";

import { joinChapter, type RoleName } from "@/api/chapters";
import { createEvent, listEvents, listRsvps, type EventOut, type EventRsvpOut } from "@/api/events";
import { likePost, listComments, listLikes, unlikePost, type PostOut } from "@/api/feed";
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
import {
  MOCK_CAMPUS,
  MOCK_CHAPTER,
  MOCK_CURRENT_MEMBERSHIP,
  MOCK_CURRENT_USER,
  MOCK_ORG_POSTS,
  MOCK_POSTS,
  mockIsOrgMember,
  mockUserById,
} from "@/mocks/data";
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

interface OrgFeedItem {
  post: PostOut;
  likeCount: number;
  commentCount: number;
  likedByMe: boolean;
}

/**
 * Feed segment (§8.7): chapter-only posts, never on the FYP. Reads BOTH the
 * legacy `source: "org"` rows in MOCK_POSTS and the dedicated MOCK_ORG_POSTS
 * (data.ts keeps them separate; this is the one place that reads both).
 */
function OrgFeedSegment() {
  const [items, setItems] = useState<OrgFeedItem[] | null>(null);

  useEffect(() => {
    const load = async () => {
      const posts = [
        ...MOCK_POSTS.filter(
          (post) => post.chapter_id === MOCK_CHAPTER.id && post.source === "org" && post.deleted_at === null,
        ),
        ...MOCK_ORG_POSTS,
      ].sort((a, b) => b.created_at.localeCompare(a.created_at));

      const withCounts = await Promise.all(
        posts.map(async (post) => {
          const [likes, comments] = await Promise.all([listLikes(post.id), listComments(post.id)]);
          return {
            post,
            likeCount: likes.length,
            commentCount: comments.length,
            likedByMe: likes.some((like) => like.user_id === MOCK_CURRENT_USER.id),
          };
        }),
      );
      setItems(withCounts);
    };
    // Fail soft: mock ids 422 against the live API until wiring lands.
    load().catch(() => setItems([]));
  }, []);

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
        message="Chapter-only posts land here — only Sigma Chi members ever see this feed."
      />
    );
  }

  return (
    <View style={{ gap: spacing.md }}>
      {(items ?? []).map((item) => {
        const author = mockUserById(item.post.author_id);
        return (
          <MediaPostCard
            key={item.post.id}
            post={item.post}
            authorName={author?.display_name ?? "Unknown"}
            authorPhotoUrl={author?.avatar_url}
            timeLabel={age(item.post.created_at)}
            likeCount={item.likeCount}
            commentCount={item.commentCount}
            likedByMe={item.likedByMe}
            onToggleLike={() => void toggleLike(item)}
          />
        );
      })}
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

interface EventWithRsvps {
  event: EventOut;
  rsvps: EventRsvpOut[];
}

/** Events segment (§8.7): event list + mock create-event sheet, wired to src/api/events.ts. */
function OrgEventsSegment() {
  const router = useRouter();
  const palette = useTheme();
  const [events, setEvents] = useState<EventWithRsvps[] | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  const reload = useCallback(async () => {
    const list = await listEvents(MOCK_CHAPTER.id);
    const withRsvps = await Promise.all(
      list.map(async (event) => ({ event, rsvps: await listRsvps(event.id) })),
    );
    setEvents(withRsvps);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const handleCreate = async (input: CreateEventInput) => {
    await createEvent(MOCK_CHAPTER.id, input);
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

/** Tools segment (§8.7): the pre-existing role-gated tool grid, unchanged, moved under this segment. */
function OrgToolsSegment({ role }: { role: RoleName }) {
  const router = useRouter();
  const palette = useTheme();
  const visible = TOOLS.filter((tool) => tool.roles === undefined || tool.roles.includes(role));

  return (
    <View style={{ gap: spacing.md }}>
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

/** Member state: org identity hero, then the Feed/Events/Tools segmented control (§8.7). */
function MemberOrgHub({
  segment,
  onSegmentChange,
}: {
  segment: OrgSegment;
  onSegmentChange: (segment: OrgSegment) => void;
}) {
  const role = MOCK_CURRENT_MEMBERSHIP.role;

  return (
    <View style={{ gap: spacing.xl }}>
      <HeroCard>
        <View style={{ gap: spacing.sm }}>
          <AppText variant="micro" tone="onAccent">
            Your org
          </AppText>
          <AppText variant="title" tone="onAccent">
            {MOCK_CHAPTER.org_name}
          </AppText>
          <AppText variant="caption" tone="onAccent">
            {MOCK_CHAPTER.chapter_name !== null
              ? `${MOCK_CHAPTER.chapter_name} · ${MOCK_CAMPUS.name}`
              : MOCK_CAMPUS.name}
          </AppText>
          <Chip label={ROLE_LABELS[role]} variant="accent" style={{ marginTop: spacing.xs }} />
        </View>
      </HeroCard>

      <OrgSegmentedControl segment={segment} onChange={onSegmentChange} />

      {segment === "feed" ? <OrgFeedSegment /> : null}
      {segment === "events" ? <OrgEventsSegment /> : null}
      {segment === "tools" ? <OrgToolsSegment role={role} /> : null}
    </View>
  );
}

/** Non-member state per DESIGN §6: invite code entry + browsable categories (greek is opt-in here). */
function FindYourOrg() {
  const palette = useTheme();
  const [code, setCode] = useState("");
  const [category, setCategory] = useState<Category>("Fraternities");

  return (
    <View style={{ gap: spacing.xl }}>
      <Card>
        <View style={{ gap: spacing.md }}>
          <AppText variant="headline">Have an invite code?</AppText>
          <TextInput
            value={code}
            onChangeText={setCode}
            placeholder="e.g. SIGCHI-EM-F26"
            placeholderTextColor={palette.inkFaint}
            autoCapitalize="characters"
            autoCorrect={false}
            style={{
              ...typography.body,
              color: palette.ink,
              backgroundColor: palette.surfaceAlt,
              borderRadius: radii.input,
              paddingHorizontal: spacing.lg,
              paddingVertical: spacing.md,
            }}
          />
          <Button
            label="Join with code"
            disabled={code.trim().length === 0}
            onPress={() => void joinChapter(code.trim())}
          />
        </View>
      </Card>

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
  const [segment, setSegment] = useState<OrgSegment>("feed");

  return (
    <View style={{ flex: 1 }}>
      <Screen
        title="Orgs"
        eyebrow={`${MOCK_CAMPUS.name.toUpperCase()} · SPARTANS`}
        accentBarColor={campusColors.secondary}
        subtitle={mockIsOrgMember ? "Your chapter, your tools." : `Find your org at ${MOCK_CAMPUS.name}`}
      >
        {mockIsOrgMember ? (
          <MemberOrgHub segment={segment} onSegmentChange={setSegment} />
        ) : (
          <FindYourOrg />
        )}
      </Screen>
      {/* Org-colored composer FAB (§8.7) — Feed segment only, mirrors Home's Fab pattern. */}
      {mockIsOrgMember && segment === "feed" ? <Fab /> : null}
    </View>
  );
}
