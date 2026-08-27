/**
 * Home = the FYP (DESIGN §7): header zone (§10.1) → filter pills → mixed-media
 * feed (MediaPostCard renders text/photo/video variants) → floating create FAB.
 *
 * The Moments row that used to sit above the pills was cut in c97: it rendered
 * seven fabricated people to every real user and had no backend behind it. See
 * the note further down for how to bring it back with a real endpoint.
 *
 * Filter pills are "For You" / "Campus" only (§8.6 — NOT "My Orgs": org posts
 * never surface on the public FYP, they live inside the org's own space,
 * §8.7). Both pills read the SAME GET /campuses/{campus_id}/feed response —
 * that endpoint only ever returns `audience: "campus"` rows, so org posts are
 * excluded by construction, no client-side filtering needed. "For You" is a
 * ranking that doesn't exist on the backend yet, so both tabs currently show
 * the identical reverse-chron list; see the `visibleItems` comment below.
 *
 * Moderation (board c35, App Store Guideline 1.2): MediaPostCard owns the
 * report/block overflow menu UI; this screen owns the actual API calls
 * (createReport, blockUser) and the post-block cleanup — dropping the
 * blocked author's posts locally, then refetching, since GET
 * /campuses/{campus_id}/feed now filters blocked authors server-side.
 */

import { useCallback, useEffect, useState } from "react";
import { Image, Pressable, View } from "react-native";
import { useRouter } from "expo-router";
import { Feather } from "@expo/vector-icons";

import { getChapter, type ChapterOut } from "@/api/chapters";
import { likePost, listCampusFeed, unlikePost, type FeedPostOut } from "@/api/feed";
import { listGuests, listMyInvites, type EventOut } from "@/api/events";
import { blockUser, createReport } from "@/api/moderation";
// useCampus (not a local getCampus fetch) — main moved campus resolution into
// SessionProvider (c67) precisely to kill the per-screen duplicate requests.
import { useCampus, useCampusAccess, useSession } from "@/auth";
import { AppText, Card, Chip, EmptyState, Fab, MediaPostCard, Screen } from "@/components";
import { showAlert, showApiError } from "@/lib/alert";
import { eventWhen } from "@/lib/dates";
import { radii, spacing, useAppearance, useTheme } from "@/theme";

type FeedFilter = "forYou" | "campus";

/** Loading: initial fetch in flight. Loaded: fetch settled, `items` reflects the
 * server (possibly genuinely empty). Error: the fetch itself failed — must never
 * be presented the same as a genuinely empty feed (that's what hid the
 * hardcoded-chapter-id bug for a week: a failed load silently rendered []). */
type LoadState = "loading" | "loaded" | "error";

/** Compact relative age for card captions ("just now", "5m", "3h", "2d"). */
function age(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

const FILTERS: { key: FeedFilter; label: string }[] = [
  { key: "forYou", label: "For You" },
  { key: "campus", label: "Campus" },
];

/** One row in the Invites section: the event plus a display label for its
 * hosting chapter, resolved separately (see loadVisibleInvites below). */
interface InviteRow {
  event: EventOut;
  chapterLabel: string;
}

/**
 * c203: GET /me/event-invites (listMyInvites) exists and has since c33/c198,
 * but nothing in the app called it — an invite is worthless if the only way
 * to learn about it is a link somebody sends you, and that hits hardest for a
 * cross-chapter invite via the 'verified' tier (c198), where the invitee has
 * no other reason to ever open that chapter's screens.
 *
 * Two shape gaps had to be worked around client-side rather than in
 * listMyInvites() itself, since backend/ is out of scope for this card:
 *
 *   - listMyInvites() returns bare EventOut, with no RSVP status. "Already
 *     responded" is derived here by calling listGuests(event.id) per invite
 *     and checking for the caller's own row — the same call the event detail
 *     screen already makes for one event. That's an N+1 the c43 bulk-RSVP
 *     pattern doesn't cover (listEventsWithRsvps is scoped to one chapter's
 *     own events, not a cross-chapter invite list), acceptable for the
 *     handful of invites one person typically holds. A future
 *     GET /me/event-invites-with-rsvps would remove it the same way c43 did.
 *   - EventOut carries chapter_id only, and GET /chapters/{id} is member-
 *     scoped (backend/app/routers/chapters.py get_chapter) — exactly the case
 *     that matters most here, since the whole feature is invites to chapters
 *     the invitee has NOT joined. Those resolve to "Another chapter" rather
 *     than a fabricated name; there is currently no route a non-member can
 *     call to learn a chapter's display name.
 *
 * Cancelled events are always kept (mirrors the backend docstring: the party
 * being off is the single most important row this can return) even if
 * already answered — everyone else is filtered down to the not-yet-answered
 * ones so a responded invite stops nagging. Sorted soonest-first on the
 * client: the endpoint itself orders starts_at descending despite its own
 * "soonest-first" docstring, which reads like a backend bug worth flagging
 * rather than one to route around silently.
 */
async function loadVisibleInvites(userId: string): Promise<InviteRow[]> {
  const events = await listMyInvites();

  const alreadyAnswered = await Promise.all(
    events.map(async (event) => {
      if (event.canceled_at !== null) return false; // cancellations always show
      try {
        const guests = await listGuests(event.id);
        return guests.rsvps.some((rsvp) => rsvp.user_id === userId);
      } catch {
        // Fail soft toward SHOWING the invite — a failed status check must
        // never be the reason a real invite silently disappears.
        return false;
      }
    }),
  );
  const visible = events
    .filter((event, index) => event.canceled_at !== null || !alreadyAnswered[index])
    .sort((a, b) => new Date(a.starts_at).getTime() - new Date(b.starts_at).getTime());

  const chapterIds = Array.from(new Set(visible.map((event) => event.chapter_id)));
  const chapterEntries = await Promise.all(
    chapterIds.map(async (chapterId): Promise<[string, ChapterOut | null]> => {
      try {
        return [chapterId, await getChapter(chapterId)];
      } catch {
        // Expected for a genuine cross-chapter invite, not just a network
        // blip — see the file comment above.
        return [chapterId, null];
      }
    }),
  );
  const chapterNames = new Map(chapterEntries);

  return visible.map((event) => {
    const chapter = chapterNames.get(event.chapter_id) ?? null;
    const chapterLabel =
      chapter === null
        ? "Another chapter"
        : chapter.chapter_name !== null
          ? `${chapter.org_name} ${chapter.chapter_name}`
          : chapter.org_name;
    return { event, chapterLabel };
  });
}

/** Invites section (c203): the discovery surface for GET /me/event-invites.
 * Home rather than the Orgs tab — the point is reaching someone who does NOT
 * follow the hosting chapter, so a surface buried inside that chapter's own
 * tab would never reach exactly the person it exists for. Renders nothing
 * while loading and nothing when there is genuinely nothing to show, rather
 * than a permanent EmptyState box on the app's main landing screen for the
 * near-universal case of zero invites. */
function InvitesSection({ userId }: { userId: string }) {
  const router = useRouter();
  const palette = useTheme();
  const [invites, setInvites] = useState<InviteRow[] | null>(null);

  const load = useCallback(() => loadVisibleInvites(userId), [userId]);

  useEffect(() => {
    // Fail soft (matches OrgEventsSegment): a failed load must not crash Home.
    void load()
      .then(setInvites)
      .catch(() => setInvites([]));
  }, [load]);

  if (invites === null || invites.length === 0) return null;

  return (
    <View style={{ marginBottom: spacing.xl, gap: spacing.md }}>
      <AppText variant="micro" tone="secondary">
        INVITES
      </AppText>
      {invites.map(({ event, chapterLabel }) => (
        <Card key={event.id} onPress={() => router.push(`/chapter/event/${event.id}`)}>
          <View style={{ flexDirection: "row", gap: spacing.md }}>
            <Image
              source={{ uri: event.cover_url }}
              style={{ width: 56, height: 56, borderRadius: radii.thumb }}
              resizeMode="cover"
            />
            <View style={{ flex: 1, gap: spacing.xs }}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
                <AppText variant="headline" numberOfLines={1} style={{ flex: 1 }}>
                  {event.title}
                </AppText>
                <Feather name="chevron-right" size={16} color={palette.inkFaint} />
              </View>
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs }}>
                <Feather name="users" size={12} color={palette.inkFaint} />
                <AppText variant="caption" tone="secondary" numberOfLines={1}>
                  {chapterLabel}
                </AppText>
              </View>
              <Chip
                label={event.canceled_at !== null ? "Canceled" : eventWhen(event.starts_at, event.ends_at)}
                variant={event.canceled_at !== null ? "danger" : "accent"}
              />
            </View>
          </View>
        </Card>
      ))}
    </View>
  );
}

export default function FeedScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { campusColors } = useAppearance();
  const { user, memberships } = useSession();
  // Single-org world (OwnChapterProvider): memberships[0] is the only chapter.
  // That provider is not mounted on this route, so derive the post target here.
  const chapterId = memberships[0]?.chapter_id ?? null;
  const [items, setItems] = useState<FeedPostOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [filter, setFilter] = useState<FeedFilter>("forYou");
  const campus = useCampus();

  // Alumni / non-campus accounts have no campus_id — there's no campus feed to
  // call the endpoint with, so that's handled as its own EmptyState below
  // rather than firing a request with a null id.
  const campusId = user?.campus_id ?? null;
  // c110: campus_id is NOT the question any more. Since c88 the campus feed is
  // gated on a verified .edu, so a chapter-joined user has a campus AND is
  // refused — branching on campusId alone walked them into a 403 that the catch
  // below rendered as a generic error.
  const access = useCampusAccess();

  const load = useCallback(async () => {
    if (campusId === null || access !== "ok") return;
    setLoadState("loading");
    try {
      const posts = await listCampusFeed(campusId);
      setItems(posts);
      setLoadState("loaded");
    } catch {
      setLoadState("error");
    }
  }, [campusId, access]);

  useEffect(() => {
    void load();
  }, [load]);

  // Real campus name for the header eyebrow: resolved once in SessionProvider
  // (c67) rather than fetched here — this screen used to run its own
  // independent GET /campuses/{id}, which was one of the redundant fetches
  // the card exists to kill. Still fails soft to null the same way: a missing
  // campus name is cosmetic and must never blank out the feed itself, which
  // is driven by campusId.

  const toggleLike = async (item: FeedPostOut) => {
    const wasLiked = item.liked_by_me;
    setItems((current) =>
      current.map((entry) =>
        entry.id === item.id
          ? { ...entry, liked_by_me: !wasLiked, like_count: entry.like_count + (wasLiked ? -1 : 1) }
          : entry,
      ),
    );
    try {
      if (wasLiked) {
        await unlikePost(item.id);
      } else {
        await likePost(item.id);
      }
    } catch {
      // Roll back the optimistic flip — previously this just never reverted,
      // leaving the UI claiming a like/unlike that the server never recorded.
      setItems((current) =>
        current.map((entry) =>
          entry.id === item.id
            ? { ...entry, liked_by_me: wasLiked, like_count: entry.like_count + (wasLiked ? 1 : -1) }
            : entry,
        ),
      );
    }
  };

  const reportPost = async (item: FeedPostOut, reason: string) => {
    try {
      await createReport({ target_type: "post", target_id: item.id, reason });
      showAlert("Reported", "Thanks for letting us know.");
    } catch (error) {
      showApiError(error, "Couldn't send that report");
    }
  };

  const blockAuthor = async (item: FeedPostOut) => {
    try {
      await blockUser(item.author_id);
      // Drop every post by this author immediately so the UI reacts at once...
      setItems((current) => current.filter((entry) => entry.author_id !== item.author_id));
      // ...then refetch, since GET /campuses/{campus_id}/feed now filters this
      // author's posts server-side (c35) — without this a later paginate/reload
      // would be the only place the block actually took effect.
      await load();
    } catch (error) {
      showApiError(error, "Couldn't block that person");
    }
  };

  // c97: the row is CUT, which was always one of the two endings this comment
  // offered. There is no backend concept of "moments" anywhere in the contract,
  // so the row rendered seven invented people — Tyler, Maria, Priya, Devon, Sam,
  // Ethan, Noah — at the top of every real user's Home, presented as their
  // campus. Alpha is Q's chapter: people who know exactly who does and does not
  // go there. Seven confident strangers is the fastest way to teach a first user
  // that nothing else on the screen is real either, and that doubt does not stay
  // contained to one row.
  //
  // Restore this by rendering a moments endpoint's response. The MOCK_MOMENTS
  // fixture and MomentsRow component are both left in place for that.

  // Both pills render the same list — see file header. `filter` only drives
  // which pill looks active until a real "For You" ranking exists server-side.
  const visibleItems = items;

  return (
    <View style={{ flex: 1 }}>
      <Screen
        title="Home"
        // Real campus name now that GET /campuses/{id} exists (c46). Undefined
        // until it resolves — an absent eyebrow beats a wrong one. The old value
        // also hardcoded "· SPARTANS", which is UNCG's mascot: wrong for every
        // other campus, and CampusOut has no mascot field to replace it with.
        eyebrow={campus ? campus.name.toUpperCase() : undefined}
        accentBarColor={campusColors.secondary}
        subtitle="Your campus, right now."
        // Fab below only renders once campusId resolves non-null — matched
        // exactly here so scroll content clears it whenever it's actually
        // showing (c168).
        hasFab={campusId !== null}
      >
        {/* Above the campus-feed gate below on purpose: an invite admits the
            invitee regardless of chapter membership or .edu verification
            (backend/app/routers/events.py _readable_event checks the invite
            before any visibility tier), so this must not disappear behind
            the "confirm your school" / "no campus feed" states further down. */}
        {user !== null ? <InvitesSection userId={user.id} /> : null}

        <View style={{ flexDirection: "row", gap: spacing.sm, marginBottom: spacing.lg }}>
          {FILTERS.map((option) => {
            const active = option.key === filter;
            return (
              <Pressable
                key={option.key}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
                onPress={() => setFilter(option.key)}
                style={({ pressed }) => ({
                  paddingHorizontal: spacing.lg,
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

        {access === "loading" ? (
          // Never flash a verify prompt at someone who turns out to be verified.
          <EmptyState title="Loading your feed..." />
        ) : access === "unverified" || access === "lapsed" ? (
          // The designed refusal c110 exists for. Distinct copy per state: a
          // returning student whose yearly re-check lapsed must not be told they
          // have never been here.
          <EmptyState
            title={access === "lapsed" ? "Confirm your school again" : "Confirm your school"}
            message={
              access === "lapsed"
                ? "It's been a year since you last confirmed your .edu address. Verify again to reopen your campus feed."
                : "Your campus feed is for students at your school. Confirm your .edu address to unlock it."
            }
            actionLabel={access === "lapsed" ? "Verify again" : "Verify my .edu"}
            onAction={() => router.push("/(auth)/verify-campus")}
          />
        ) : campusId === null ? (
          // c96: this used to be a flat dead end — it stated a fact and offered
          // nothing, because at the time there was genuinely no way to get a
          // campus. Joining an org now grants one (server-derived from the
          // chapter), so the copy can finally name a way out.
          <EmptyState
            title="No campus feed"
            message="Join your org from the Orgs tab and your campus feed unlocks with it."
          />
        ) : loadState === "error" ? (
          <EmptyState
            title="Couldn't load your feed"
            message="Something went wrong reaching the server."
            actionLabel="Try again"
            onAction={() => void load()}
          />
        ) : loadState === "loading" ? (
          <EmptyState title="Loading your feed..." />
        ) : visibleItems.length === 0 ? (
          <EmptyState title="Nothing here yet" message="Posts from your campus will show up here." />
        ) : (
          <View style={{ gap: spacing.md }}>
            {visibleItems.map((item) => (
              <MediaPostCard
                key={item.id}
                post={item}
                authorName={item.display_name}
                authorPhotoUrl={item.avatar_url}
                timeLabel={age(item.created_at)}
                likeCount={item.like_count}
                commentCount={item.comment_count}
                likedByMe={item.liked_by_me}
                onToggleLike={() => void toggleLike(item)}
                onReport={(reason) => void reportPost(item, reason)}
                onBlock={() => void blockAuthor(item)}
                canBlock={user !== null && item.author_id !== user.id}
              />
            ))}
          </View>
        )}
      </Screen>
      {/* org-audience posts never appear here — GET /campuses/{id}/feed is campus-only, so onPosted refreshing with no new row is correct. */}
      {campusId !== null ? (
        <Fab
          chapterId={chapterId}
          campusId={campusId}
          campusName={campus?.name ?? null}
          isActiveMember={memberships[0]?.status === "active"}
          onPosted={() => void load()}
        />
      ) : null}
    </View>
  );
}
