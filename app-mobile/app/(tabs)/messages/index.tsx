/** Messages: conversation list — titles from group name or DM counterpart, mock previews. */

import { useRouter } from "expo-router";
import { useEffect, useState } from "react";

import { listConversations, listMessages, type ConversationOut } from "@/api/messages";
import { Avatar, Card, EmptyState, ListRow, Screen } from "@/components";
import { MOCK_CURRENT_USER, mockUserById, type MockMessage } from "@/mocks/data";

interface ConversationItem {
  conversation: ConversationOut;
  title: string;
  preview: string;
}

function conversationTitle(conversation: ConversationOut): string {
  if (conversation.kind === "group") return conversation.title ?? "Group";
  const other = (conversation.members ?? []).find((m) => m.user_id !== MOCK_CURRENT_USER.id);
  return other ? (mockUserById(other.user_id)?.display_name ?? "Direct message") : "Direct message";
}

export default function MessagesScreen() {
  const router = useRouter();
  const [items, setItems] = useState<ConversationItem[] | null>(null);

  useEffect(() => {
    const load = async () => {
      const conversations = await listConversations();
      const withPreviews = await Promise.all(
        conversations.map(async (conversation) => {
          const messages = await listMessages(conversation.id);
          // Mock world only: MessageOut rows carry mock_plaintext so the UI can render.
          // Real previews come from the on-device decrypted store (milestone 4).
          const last = messages[messages.length - 1] as MockMessage | undefined;
          return {
            conversation,
            title: conversationTitle(conversation),
            preview: last?.mock_plaintext ?? "No messages yet",
          };
        }),
      );
      setItems(withPreviews);
    };
    void load();
  }, []);

  return (
    <Screen title="Messages" subtitle="End-to-end encrypted — E2EE lands in milestone 4">
      {items !== null && items.length === 0 ? (
        <EmptyState title="No conversations" message="Start a DM or group with your chapter." />
      ) : (
        <Card>
          {(items ?? []).map((item, index) => (
            <ListRow
              key={item.conversation.id}
              title={item.title}
              subtitle={item.preview}
              left={<Avatar name={item.title} />}
              divider={index < (items ?? []).length - 1}
              onPress={() => router.push(`/messages/${item.conversation.id}`)}
            />
          ))}
        </Card>
      )}
    </Screen>
  );
}
