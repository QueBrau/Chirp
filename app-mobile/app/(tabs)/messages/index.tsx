/**
 * Messages: conversation rows per DESIGN §7 — GradientAvatar 48, headline name,
 * "Message" encrypted preview caption. Plaintext previews only exist after the
 * on-device decrypted store lands (milestone 4); an unread indicator would need
 * a real read-receipt signal this build doesn't have, so it's omitted rather
 * than faked.
 *
 * c273: entry point into messages/new.tsx. Not a FAB — DESIGN §7 is explicit
 * ("One FAB, Home only") — and Screen has no header-action slot to add one to
 * (it's a shared component, see CLAUDE.md). NewConversationButton instead
 * follows the precedent profile/index.tsx already set for a header-adjacent
 * action: a small ghost pill defined locally in the screen file and rendered
 * inside `children`, right under the header.
 */

import { useRouter } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import { Feather } from "@expo/vector-icons";

import { listConversations, type ConversationOut } from "@/api/messages";
import { AppText, Button, Card, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { chirpSocket, isMessageEvent } from "@/realtime/socket";
import { radii, spacing, typography, useTheme } from "@/theme";

/** One inbox page (board c344) — matches the backend's default/max shape (30/100). */
const PAGE_LIMIT = 30;

/** Header-adjacent ghost pill, same shape as profile/index.tsx's EditLayoutToggle. */
function NewConversationButton({ onPress }: { onPress: () => void }) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="Start a new conversation"
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.xs,
        paddingHorizontal: spacing.lg,
        paddingVertical: spacing.sm,
        borderRadius: radii.pill,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather name="edit" size={typography.caption.fontSize} color={palette.inkSecondary} />
      <AppText variant="bodyBold" tone="secondary">
        New
      </AppText>
    </Pressable>
  );
}

interface ConversationItem {
  conversation: ConversationOut;
  title: string;
  preview: string;
}

/** No GET /users/{id} exists, so a titleless DM can't resolve the other
 * participant's name — fall back to a neutral, non-fabricated label. */
function conversationTitle(conversation: ConversationOut): string {
  if (conversation.title) return conversation.title;
  return conversation.kind === "group" ? "Group" : "Direct message";
}

/** Real bodies are ciphertext only — the row shows the encrypted preview
 * ("Message") until on-device decryption lands. has_messages (board c344) comes
 * straight off the conversation row, replacing the old per-row history-fetch call
 * that fetched an entire message page just to answer this one boolean. */
function toItem(conversation: ConversationOut): ConversationItem {
  return {
    conversation,
    title: conversationTitle(conversation),
    preview: conversation.has_messages ? "Message" : "No messages yet",
  };
}

export default function MessagesScreen() {
  const router = useRouter();
  const palette = useTheme();
  const [items, setItems] = useState<ConversationItem[] | null>(null);
  /** The inbox fetch failed. Distinct from a genuinely empty inbox (c299) - the two
   * used to render identically, so a dropped request told the user they had no
   * conversations. Same rule feed/index.tsx's LoadState comment sets out. */
  const [loadFailed, setLoadFailed] = useState(false);
  // Whether the last page fetched was full — a page shorter than PAGE_LIMIT is
  // provably the last one (board c344's cursor pagination).
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  // Hoisted from the mount effect (c304) so pull-to-refresh can invoke it too.
  const load = useCallback(async () => {
    try {
      const conversations = await listConversations({ limit: PAGE_LIMIT });
      setItems(conversations.map(toItem));
      setHasMore(conversations.length === PAGE_LIMIT);
      setLoadFailed(false);
    } catch {
      // NOT `.catch(() => setItems([]))` (c299): an empty array is the server's answer
      // "you have no conversations", and a failed fetch has no answer at all. Rendering
      // them the same is the bug this rollout removes — the failure gets its own state.
      //
      // Handled INSIDE load rather than at the call site, because c304 hoisted this
      // into a useCallback that pull-to-refresh also invokes directly (onRefresh={load}).
      // A catch on only the mount effect would leave a failed PULL unhandled.
      setLoadFailed(true);
    }
  }, []);

  // Older page via the (created_at, id) cursor — the same compound cursor
  // listMessages already uses, now on GET /conversations too (board c344).
  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore || items === null || items.length === 0) return;
    setLoadingMore(true);
    try {
      const last = items[items.length - 1].conversation;
      const older = await listConversations({
        before: last.created_at,
        before_id: last.id,
        limit: PAGE_LIMIT,
      });
      setItems((current) => [...(current ?? []), ...older.map(toItem)]);
      setHasMore(older.length === PAGE_LIMIT);
    } catch {
      // Fail soft: the page already on screen stays usable; pull-to-refresh retries.
    } finally {
      setLoadingMore(false);
    }
  }, [items, hasMore, loadingMore]);

  useEffect(() => {
    void load();

    // c63: flip a row from "No messages yet" to "Message" the moment
    // something arrives, rather than only on the next full mount of this
    // screen. No reordering and no real preview text — those need real
    // content decrypted (m4) or a recency sort this screen doesn't have
    // today; this only updates the ONE thing that changed and is knowable
    // without either.
    const unsubEvent = chirpSocket.onEvent((event) => {
      if (!isMessageEvent(event)) return;
      setItems((current) =>
        (current ?? []).map((item) =>
          item.conversation.id === event.conversation_id
            ? { ...item, preview: "Message" }
            : item,
        ),
      );
    });

    return unsubEvent;
  }, [load]);

  return (
    <Screen
      title="Messages"
      subtitle="Start conversations. Sending isn't available yet."
      onRefresh={load}
    >
      <View style={{ alignItems: "flex-end", marginBottom: spacing.sm }}>
        <NewConversationButton onPress={() => router.push("/messages/new")} />
      </View>
      {loadFailed ? (
        // Checked FIRST: a failure must never fall through to copy that asserts
        // something about the user's actual inbox.
        <EmptyState
          title="Couldn't load your messages"
          message="Check your connection and try again. This isn't a statement that you have none."
        />
      ) : items !== null && items.length === 0 ? (
        <EmptyState
          title="No conversations"
          message="Start a DM or group with your chapter."
          actionLabel="Start a conversation"
          onAction={() => router.push("/messages/new")}
        />
      ) : (
        <View style={{ gap: spacing.md }}>
          <Card>
            {(items ?? []).map((item, index) => (
              <ListRow
                key={item.conversation.id}
                title={item.title}
                subtitle={
                  <AppText variant="caption" tone="secondary" numberOfLines={1}>
                    <Feather name="lock" size={12} color={palette.inkFaint} /> {item.preview}
                  </AppText>
                }
                left={<GradientAvatar name={item.title} size={48} />}
                divider={index < (items ?? []).length - 1}
                onPress={() => router.push(`/messages/${item.conversation.id}`)}
              />
            ))}
          </Card>
          {hasMore ? (
            <Button
              label={loadingMore ? "Loading conversations..." : "Load older conversations"}
              variant="secondary"
              disabled={loadingMore}
              onPress={() => void loadMore()}
            />
          ) : null}
        </View>
      )}
    </Screen>
  );
}
