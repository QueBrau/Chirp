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
import { Alert, Pressable, View } from "react-native";

import { ApiError } from "@/api/client";
import { likePost, listCampusFeed, unlikePost, type FeedPostOut } from "@/api/feed";
import { blockUser, createReport } from "@/api/moderation";
// useCampus (not a local getCampus fetch) — main moved campus resolution into
// SessionProvider (c67) precisely to kill the per-screen duplicate requests.
import { useCampus, useSession } from "@/auth";
import { AppText, EmptyState, Fab, MediaPostCard, Screen } from "@/components";
import { radii, spacing, useAppearance, useTheme } from "@/theme";

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

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

export default function FeedScreen() {
  const palette = useTheme();
  const { campusColors } = useAppearance();
  const { user } = useSession();
  const [items, setItems] = useState<FeedPostOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [filter, setFilter] = useState<FeedFilter>("forYou");
  const campus = useCampus();

  // Alumni / non-campus accounts have no campus_id — there's no campus feed to
  // call the endpoint with, so that's handled as its own EmptyState below
  // rather than firing a request with a null id.
  const campusId = user?.campus_id ?? null;

  const load = useCallback(async () => {
    if (campusId === null) return;
    setLoadState("loading");
    try {
      const posts = await listCampusFeed(campusId);
      setItems(posts);
      setLoadState("loaded");
    } catch {
      setLoadState("error");
    }
  }, [campusId]);

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
      Alert.alert("Reported", "Thanks for letting us know.");
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
      >
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

        {campusId === null ? (
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
      {campusId !== null ? <Fab /> : null}
    </View>
  );
}
