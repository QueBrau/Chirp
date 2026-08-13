/**
 * Home = the FYP (DESIGN §7): Moments row → filter pills ("For You" / "Campus"
 * / "My Orgs", filters mock posts by `source`) → mixed-media feed
 * (MediaPostCard renders text/photo/video variants) → floating create FAB.
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
import { radii, spacing, useTheme } from "@/theme";

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
  { key: "org", label: "My Orgs" },
];

export default function FeedScreen() {
  const palette = useTheme();
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
    void load();
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
      MOCK_MOMENTS.map((moment) => ({
        id: moment.id,
        name: mockUserById(moment.userId)?.display_name.split(" ")[0] ?? "Friend",
      })),
    [],
  );

  const visibleItems = (items ?? []).filter((item) => (item.post.source ?? "org") === filter);

  return (
    <View style={{ flex: 1 }}>
      <Screen title="Home" subtitle={MOCK_CAMPUS.name}>
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
