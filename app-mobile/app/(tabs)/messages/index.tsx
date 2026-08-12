/**
 * Messages: conversation rows per DESIGN §7 — GradientAvatar 48, headline name,
 * "Message" encrypted preview caption, unread accent dot. Plaintext previews
 * only exist after the on-device decrypted store lands (milestone 4).
 */

import { useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { View } from "react-native";

import { Feather } from "@expo/vector-icons";

import { listConversations, listMessages, type ConversationOut } from "@/api/messages";
import { AppText, Card, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { MOCK_CURRENT_USER, mockUserById, type MockMessage } from "@/mocks/data";
import { spacing, useTheme } from "@/theme";

interface ConversationItem {
  conversation: ConversationOut;
  title: string;
  preview: string;
  /** Mock heuristic: last message exists and was sent by someone else. */
  unread: boolean;
}

function conversationTitle(conversation: ConversationOut): string {
  if (conversation.kind === "group") return conversation.title ?? "Group";
  const other = (conversation.members ?? []).find((m) => m.user_id !== MOCK_CURRENT_USER.id);
  return other ? (mockUserById(other.user_id)?.display_name ?? "Direct message") : "Direct message";
}

/** Unread indicator per §7: small accent dot on the row's trailing edge. */
function UnreadDot() {
  const palette = useTheme();
  return (
    <View
      style={{
        width: spacing.sm,
        height: spacing.sm,
        borderRadius: spacing.sm,
        backgroundColor: palette.accent,
      }}
    />
  );
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
          // Mock world only: rows are MockMessage. The UI still shows the encrypted
          // preview ("Message") — real previews come from on-device decryption.
          const last = messages[messages.length - 1] as MockMessage | undefined;
          return {
            conversation,
            title: conversationTitle(conversation),
            preview: last ? "Message" : "No messages yet",
            unread: last !== undefined && last.sender_user_id !== MOCK_CURRENT_USER.id,
          };
        }),
      );
      setItems(withPreviews);
    };
    void load();
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
              right={item.unread ? <UnreadDot /> : undefined}
              divider={index < (items ?? []).length - 1}
              onPress={() => router.push(`/messages/${item.conversation.id}`)}
            />
          ))}
        </Card>
      )}
    </Screen>
  );
}
