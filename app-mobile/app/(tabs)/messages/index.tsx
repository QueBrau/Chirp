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
import { useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import { Feather } from "@expo/vector-icons";

import { listConversations, listMessages, type ConversationOut } from "@/api/messages";
import { AppText, Card, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { chirpSocket, isMessageEvent } from "@/realtime/socket";
import { radii, spacing, typography, useTheme } from "@/theme";

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

export default function MessagesScreen() {
  const router = useRouter();
  const palette = useTheme();
  const [items, setItems] = useState<ConversationItem[] | null>(null);

  useEffect(() => {
    const load = async () => {
      const conversations = await listConversations();
      const withPreviews = await Promise.all(
        conversations.map(async (conversation) => {
          const messages = await listMessages(conversation.id);
          // Real bodies are ciphertext only — the row shows the encrypted
          // preview ("Message") until on-device decryption lands.
          const last = messages[messages.length - 1];
          return {
            conversation,
            title: conversationTitle(conversation),
            preview: last ? "Message" : "No messages yet",
          };
        }),
      );
      setItems(withPreviews);
    };
    // Fail soft: matches the repo pattern elsewhere in this stack.
    load().catch(() => setItems([]));

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
  }, []);

  return (
    <Screen title="Messages" subtitle="Start conversations. Sending isn't available yet.">
      <View style={{ alignItems: "flex-end", marginBottom: spacing.sm }}>
        <NewConversationButton onPress={() => router.push("/messages/new")} />
      </View>
      {items !== null && items.length === 0 ? (
        <EmptyState
          title="No conversations"
          message="Start a DM or group with your chapter."
          actionLabel="Start a conversation"
          onAction={() => router.push("/messages/new")}
        />
      ) : (
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
      )}
    </Screen>
  );
}
