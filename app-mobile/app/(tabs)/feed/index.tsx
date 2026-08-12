/** Home: reverse-chron chapter posts — GradientAvatar author rows, like/comment action row (DESIGN §7). */

import { Feather } from "@expo/vector-icons";
import { useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import {
  likePost,
  listComments,
  listLikes,
  listPosts,
  unlikePost,
  type PostOut,
} from "@/api/feed";
import { AppText, Card, EmptyState, GradientAvatar, Screen } from "@/components";
import { MOCK_CHAPTER, MOCK_CURRENT_MEMBERSHIP, MOCK_CURRENT_USER, mockUserById } from "@/mocks/data";
import { spacing, typography, useTheme } from "@/theme";

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

const SUBTITLE = MOCK_CHAPTER.chapter_name
  ? `${MOCK_CHAPTER.org_name} · ${MOCK_CHAPTER.chapter_name}`
  : MOCK_CHAPTER.org_name;

export default function FeedScreen() {
  const palette = useTheme();
  const [items, setItems] = useState<FeedItem[] | null>(null);

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

  return (
    <Screen title="Home" subtitle={SUBTITLE}>
      {items !== null && items.length === 0 ? (
        <EmptyState
          title="Nothing yet"
          message="Posts from your chapter will show up here."
        />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(items ?? []).map((item) => {
            const { post, likeCount, commentCount, likedByMe } = item;
            const author = mockUserById(post.author_id);
            const authorName = author?.display_name ?? "Unknown";
            return (
              <Card key={post.id}>
                <View style={{ gap: spacing.md }}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
                    <GradientAvatar name={authorName} size={40} />
                    <View style={{ flex: 1, gap: spacing.xs }}>
                      <AppText variant="headline" numberOfLines={1}>
                        {authorName}
                      </AppText>
                      <AppText variant="caption" tone="tertiary">
                        {age(post.created_at)}
                      </AppText>
                    </View>
                  </View>

                  <AppText>{post.body}</AppText>

                  {/* Action row per §7: heart / message-circle counts in inkFaint, liked state accent. */}
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xl }}>
                    <Pressable
                      accessibilityRole="button"
                      accessibilityLabel={likedByMe ? "Unlike" : "Like"}
                      onPress={() => void toggleLike(item)}
                      hitSlop={spacing.sm}
                      style={({ pressed }) => ({
                        flexDirection: "row",
                        alignItems: "center",
                        gap: spacing.xs,
                        opacity: pressed ? 0.7 : 1,
                      })}
                    >
                      <Feather
                        name="heart"
                        size={typography.bodyBold.fontSize}
                        color={likedByMe ? palette.accent : palette.inkFaint}
                      />
                      <AppText variant="caption" tone={likedByMe ? "accent" : "tertiary"}>
                        {likeCount}
                      </AppText>
                    </Pressable>
                    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs }}>
                      <Feather name="message-circle" size={typography.caption.fontSize} color={palette.inkFaint} />
                      <AppText variant="caption" tone="tertiary">
                        {commentCount}
                      </AppText>
                    </View>
                  </View>
                </View>
              </Card>
            );
          })}
        </View>
      )}
    </Screen>
  );
}
