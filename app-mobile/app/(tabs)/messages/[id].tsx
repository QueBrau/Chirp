/** Thread view: mock message bubbles; composer disabled until E2EE (milestone 4). */

import { useLocalSearchParams } from "expo-router";
import { useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";

import { listConversations, listMessages, type ConversationOut } from "@/api/messages";
import { AppText, Screen } from "@/components";
import { MOCK_CURRENT_USER, mockUserById, type MockMessage } from "@/mocks/data";
import { radii, spacing, useTheme } from "@/theme";

function bubbleTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export default function ThreadScreen() {
  const palette = useTheme();
  const { id } = useLocalSearchParams<{ id: string }>();
  const [conversation, setConversation] = useState<ConversationOut | null>(null);
  const [messages, setMessages] = useState<MockMessage[]>([]);

  useEffect(() => {
    const load = async () => {
      const conversations = await listConversations();
      setConversation(conversations.find((c) => c.id === id) ?? null);
      // Mock world: rows are MockMessage (carry sender_user_id + mock_plaintext so the
      // scaffold can render). Real plaintext only ever exists in on-device SQLite (SPEC §2.1).
      const history = (await listMessages(id)) as MockMessage[];
      setMessages(history);
    };
    void load();
  }, [id]);

  const title =
    conversation?.kind === "group"
      ? (conversation.title ?? "Group")
      : (mockUserById(
          (conversation?.members ?? []).find((m) => m.user_id !== MOCK_CURRENT_USER.id)?.user_id ??
            "",
        )?.display_name ?? "Conversation");

  return (
    <Screen title={title} subtitle={conversation?.kind === "group" ? "Group" : "Direct message"}>
      <View style={{ gap: spacing.sm }}>
        {messages.map((message) => {
          const mine = message.sender_user_id === MOCK_CURRENT_USER.id;
          const sender = mockUserById(message.sender_user_id);
          return (
            <View
              key={message.id}
              style={{
                alignSelf: mine ? "flex-end" : "flex-start",
                maxWidth: "82%",
                gap: spacing.xs,
              }}
            >
              {!mine && conversation?.kind === "group" ? (
                <AppText variant="caption" tone="tertiary" style={{ marginLeft: spacing.sm }}>
                  {sender?.display_name ?? "Unknown"}
                </AppText>
              ) : null}
              <View
                style={{
                  backgroundColor: mine ? palette.accent : palette.surface,
                  borderWidth: mine ? 0 : StyleSheet.hairlineWidth,
                  borderColor: palette.border,
                  borderRadius: radii.lg,
                  paddingHorizontal: spacing.lg,
                  paddingVertical: spacing.sm,
                }}
              >
                <AppText tone={mine ? "onAccent" : "primary"}>{message.mock_plaintext}</AppText>
              </View>
              <AppText
                variant="caption"
                tone="tertiary"
                style={{ alignSelf: mine ? "flex-end" : "flex-start" }}
              >
                {bubbleTime(message.created_at)}
              </AppText>
            </View>
          );
        })}

        {/* Composer placeholder — sending requires the libsignal pipeline. TODO(milestone-4). */}
        <View
          style={{
            marginTop: spacing.lg,
            backgroundColor: palette.surface,
            borderWidth: StyleSheet.hairlineWidth,
            borderColor: palette.border,
            borderRadius: radii.pill,
            paddingHorizontal: spacing.lg,
            paddingVertical: spacing.md,
            opacity: 0.6,
          }}
        >
          <AppText tone="tertiary">Message composer unlocks with E2EE (milestone 4)</AppText>
        </View>
      </View>
    </Screen>
  );
}
