/**
 * Thread view per DESIGN §7: bubbles — mine = accent bg / white text, radius 20
 * with a 6 tail corner; theirs = surfaceAlt / ink. Pill composer + accent circular
 * send, both disabled until the libsignal pipeline lands (milestone 4).
 */

import { useLocalSearchParams } from "expo-router";
import { useEffect, useState } from "react";
import { TextInput, View } from "react-native";

import { listConversations, listMessages, type ConversationOut } from "@/api/messages";
import { AppText, Screen } from "@/components";
import { MOCK_CURRENT_USER, mockUserById, type MockMessage } from "@/mocks/data";
import { metrics, radii, spacing, typography, useTheme } from "@/theme";

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
    <Screen
      title={title}
      subtitle={conversation?.kind === "group" ? "Group · encrypted" : "Direct message · encrypted"}
    >
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
                <AppText variant="caption" tone="tertiary" style={{ marginLeft: spacing.md }}>
                  {sender?.display_name ?? "Unknown"}
                </AppText>
              ) : null}
              <View
                style={{
                  backgroundColor: mine ? palette.accent : palette.surfaceAlt,
                  borderRadius: radii.card,
                  // §7 tail corner: 6 (radii.sm) on the sender's side.
                  borderBottomRightRadius: mine ? radii.sm : radii.card,
                  borderBottomLeftRadius: mine ? radii.card : radii.sm,
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

        {/* Composer per §7: pill input + accent circular send — visually present,
            disabled until the libsignal pipeline lands. TODO(milestone-4). */}
        <View style={{ marginTop: spacing.lg, gap: spacing.sm }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            <TextInput
              editable={false}
              placeholder="Message"
              placeholderTextColor={palette.inkFaint}
              style={{
                flex: 1,
                height: metrics.buttonHeight,
                borderRadius: radii.pill,
                backgroundColor: palette.surfaceAlt,
                paddingHorizontal: spacing.lg,
                fontSize: typography.body.fontSize,
                color: palette.ink,
              }}
            />
            <View
              accessibilityRole="button"
              accessibilityState={{ disabled: true }}
              style={{
                width: metrics.buttonHeight,
                height: metrics.buttonHeight,
                borderRadius: radii.pill,
                backgroundColor: palette.accent,
                alignItems: "center",
                justifyContent: "center",
                opacity: 0.4,
              }}
            >
              <AppText variant="headline" tone="onAccent">
                ➤
              </AppText>
            </View>
          </View>
          <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
            🔒 Sending unlocks with E2EE (milestone 4)
          </AppText>
        </View>
      </View>
    </Screen>
  );
}
