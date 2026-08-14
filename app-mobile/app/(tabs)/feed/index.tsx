/**
 * Home = the FYP (DESIGN §7): header zone (§10.1) → Moments row → filter
 * pills → mixed-media feed (MediaPostCard renders text/photo/video variants)
 * → floating create FAB.
 *
 * Filter pills are "For You" / "Campus" only (§8.6 — NOT "My Orgs": org posts
 * never surface on the public FYP, they live inside the org's own space,
 * §8.7). Since MOCK_POSTS tagged `source: "org"` simply have no matching
 * filter here, they're excluded from Home by construction — no separate
 * filtering step needed, and no org stripe/Chip belongs on this screen either.
 */

import { useEffect, useMemo, useState } from "react";
import { Pressable, View } from "react-native";

import {
  likePost,
  listComments,
  listLikes,
  listPosts,
  unlikePost,
  type PostOut,
  type PostSource,
} from "@/api/feed";
import { AppText, EmptyState, Fab, MediaPostCard, MomentsRow, Screen } from "@/components";
import { MOCK_CAMPUS, MOCK_CURRENT_MEMBERSHIP, MOCK_CURRENT_USER, MOCK_MOMENTS, mockUserById } from "@/mocks/data";
import { radii, spacing, useAppearance, useTheme } from "@/theme";

interface FeedItem {
  post: PostOut;
  likeCount: number;
  commentCount: number;
  likedByMe: boolean;
}

/** Compact relative age for card captions ("just now", "5m", "3h", "2d"). */
function age(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

const FILTERS: { key: PostSource; label: string }[] = [
  { key: "forYou", label: "For You" },
  { key: "campus", label: "Campus" },
];

export default function FeedScreen() {
  const palette = useTheme();
  const { campusColors } = useAppearance();
  const [items, setItems] = useState<FeedItem[] | null>(null);
  const [filter, setFilter] = useState<PostSource>("forYou");

  useEffect(() => {
    const load = async () => {
      const posts = await listPosts(MOCK_CURRENT_MEMBERSHIP.chapter_id);
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
    // Fail soft until c34 wires real ids: mock chapter ids 422 against the live
    // API, and a crash here takes down the whole tab shell.
    load().catch(() => setItems([]));
  }, []);

  const toggleLike = async (item: FeedItem) => {
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

  const moments = useMemo(
    () =>
      MOCK_MOMENTS.map((moment) => {
        const user = mockUserById(moment.userId);
        return {
          id: moment.id,
          name: user?.display_name.split(" ")[0] ?? "Friend",
          photoUrl: user?.avatar_url,
        };
      }),
    [],
  );

  const visibleItems = (items ?? []).filter((item) => item.post.source === filter);

  return (
    <View style={{ flex: 1 }}>
      <Screen
        title="Home"
        eyebrow={`${MOCK_CAMPUS.name.toUpperCase()} · SPARTANS`}
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

        {items !== null && visibleItems.length === 0 ? (
          <EmptyState title="Nothing here yet" message="Posts matching this filter will show up here." />
        ) : (
          <View style={{ gap: spacing.md }}>
            {visibleItems.map((item) => {
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
        )}
      </Screen>
      <Fab />
    </View>
  );
}
