/**
 * Home = the FYP (DESIGN §7): header zone (§10.1) → Moments row → filter
 * pills → mixed-media feed (MediaPostCard renders text/photo/video variants)
 * → floating create FAB.
 *
 * Filter pills are "For You" / "Campus" only (§8.6 — NOT "My Orgs": org posts
 * never surface on the public FYP, they live inside the org's own space,
 * §8.7). Both pills read the SAME GET /campuses/{campus_id}/feed response —
 * that endpoint only ever returns `audience: "campus"` rows, so org posts are
 * excluded by construction, no client-side filtering needed. "For You" is a
 * ranking that doesn't exist on the backend yet, so both tabs currently show
 * the identical reverse-chron list; see the `visibleItems` comment below.
 */

import { useCallback, useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import { getCampus, type CampusOut } from "@/api/auth";
import { likePost, listCampusFeed, unlikePost, type FeedPostOut } from "@/api/feed";
import { useSession } from "@/auth";
import { AppText, EmptyState, Fab, MediaPostCard, MomentsRow, Screen } from "@/components";
import { MOCK_MOMENTS, mockUserById } from "@/mocks/data";
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

export default function FeedScreen() {
  const palette = useTheme();
  const { campusColors } = useAppearance();
  const { user, memberships } = useSession();
  // Single-org world (OwnChapterProvider): memberships[0] is the only chapter.
  // That provider is not mounted on this route, so derive the post target here.
  const chapterId = memberships[0]?.chapter_id ?? null;
  const [items, setItems] = useState<FeedPostOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [filter, setFilter] = useState<FeedFilter>("forYou");
  const [campus, setCampus] = useState<CampusOut | null>(null);

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

  // Real campus name for the header eyebrow (GET /campuses/{id}, added with c46).
  // Fails soft to null: a missing campus name is cosmetic and must never blank
  // out the feed itself, which is driven by campusId.
  useEffect(() => {
    if (campusId === null) {
      setCampus(null);
      return;
    }
    getCampus(campusId)
      .then(setCampus)
      .catch(() => setCampus(null));
  }, [campusId]);

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

  // Moments row: MOCK ONLY, always — there is no backend concept of "moments"
  // anywhere in the contract. This is the last mock data rendered in the app
  // (the USE_MOCKS layer itself was deleted in PR #8); it stays until either a
  // moments endpoint exists or the row is cut. Left explicit rather than
  // inventing an endpoint to hide it.
  const moments = MOCK_MOMENTS.map((moment) => {
    const momentUser = mockUserById(moment.userId);
    return {
      id: moment.id,
      name: momentUser?.display_name.split(" ")[0] ?? "Friend",
      photoUrl: momentUser?.avatar_url,
    };
  });

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
        <View style={{ marginBottom: spacing.lg }}>
          <MomentsRow moments={moments} />
        </View>

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
          <EmptyState
            title="No campus feed"
            message="Your account isn't linked to a campus, so there's nothing to show here."
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
              />
            ))}
          </View>
        )}
      </Screen>
      {/* org-audience posts never appear here — GET /campuses/{id}/feed is campus-only, so onPosted refreshing with no new row is correct. */}
      {campusId !== null ? (
        <Fab chapterId={chapterId} campusName={campus?.name ?? null} onPosted={() => void load()} />
      ) : null}
    </View>
  );
}
