/**
 * Messages: conversation rows per DESIGN §7 — GradientAvatar 48, headline name,
 * "Message" encrypted preview caption. Plaintext previews only exist after the
 * on-device decrypted store lands (milestone 4); an unread indicator would need
 * a real read-receipt signal this build doesn't have, so it's omitted rather
 * than faked.
 */

import { useRouter } from "expo-router";
import { useEffect, useState } from "react";

import { Feather } from "@expo/vector-icons";

import { listConversations, listMessages, type ConversationOut } from "@/api/messages";
import { AppText, Card, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { useTheme } from "@/theme";

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
  }, []);

  return (
    <Screen title="Messages" subtitle="End-to-end encrypted">
      {items !== null && items.length === 0 ? (
        <EmptyState
          title="No conversations"
          message="Start a DM or group with your chapter."
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
