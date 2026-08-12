/** Feed: reverse-chron chapter posts with like/comment counts (v1, SPEC §5). */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listComments, listLikes, listPosts, type PostOut } from "@/api/feed";
import { AppText, Avatar, Card, EmptyState, Screen } from "@/components";
import { MOCK_CHAPTER, MOCK_CURRENT_MEMBERSHIP, mockUserById } from "@/mocks/data";
import { spacing } from "@/theme";

interface FeedItem {
  post: PostOut;
  likeCount: number;
  commentCount: number;
}

function timestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export default function FeedScreen() {
  const [items, setItems] = useState<FeedItem[] | null>(null);

  useEffect(() => {
    const load = async () => {
      const posts = await listPosts(MOCK_CURRENT_MEMBERSHIP.chapter_id);
      const withCounts = await Promise.all(
        posts.map(async (post) => {
          const [likes, comments] = await Promise.all([listLikes(post.id), listComments(post.id)]);
          return { post, likeCount: likes.length, commentCount: comments.length };
        }),
      );
      setItems(withCounts);
    };
    void load();
  }, []);

  return (
    <Screen title="Feed" subtitle={`${MOCK_CHAPTER.org_name} — ${MOCK_CHAPTER.chapter_name ?? ""}`}>
      {items !== null && items.length === 0 ? (
        <EmptyState title="Nothing yet" message="Posts from your chapter will show up here." />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(items ?? []).map(({ post, likeCount, commentCount }) => {
            const author = mockUserById(post.author_id);
            return (
              <Card key={post.id}>
                <View style={{ gap: spacing.md }}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
                    <Avatar name={author?.display_name ?? "?"} size={36} />
                    <View style={{ flex: 1 }}>
                      <AppText style={{ fontWeight: "600" }}>
                        {author?.display_name ?? "Unknown"}
                      </AppText>
                      <AppText variant="caption" tone="tertiary">
                        {timestamp(post.created_at)}
                      </AppText>
                    </View>
                  </View>
                  <AppText>{post.body}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {likeCount} {likeCount === 1 ? "like" : "likes"} · {commentCount}{" "}
                    {commentCount === 1 ? "comment" : "comments"}
                  </AppText>
                </View>
              </Card>
            );
          })}
        </View>
      )}
    </Screen>
  );
}
