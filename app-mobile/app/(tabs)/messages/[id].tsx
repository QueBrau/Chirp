/**
 * Thread view per DESIGN §7: bubbles use the accent/surfaceAlt palette with a
 * 6/20 tail-corner radius. Real MessageOut carries only ciphertext_b64 (no
 * plaintext) and sender_device_id (no sender_user_id), so this build can
 * neither decrypt a body nor tell "mine" from "theirs" — every bubble renders
 * the same designed "encrypted" placeholder instead of faking either. Pill
 * composer + accent circular send stay disabled until the libsignal pipeline
 * lands (milestone 4).
 */

import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Feather } from "@expo/vector-icons";
import { useCallback, useMemo, useRef, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import {
  getConversation,
  getMessagesById,
  leaveConversation,
  listMessages,
  type ConversationOut,
  type MessageOut,
} from "@/api/messages";
import { useSession } from "@/auth";
import { currentIdentity } from "@/auth/identity";
import { DurableWindow } from "@/realtime/durableWindow";
import { AppText, Button, EmptyState, Screen } from "@/components";
import { confirmAction, showApiError } from "@/lib/alert";
import { chirpSocket, isMessageEvent } from "@/realtime/socket";
import { metrics, radii, spacing, typography, useTheme } from "@/theme";

/** Header-adjacent ghost pill, same shape as messages/index.tsx's
 * NewConversationButton and profile/index.tsx's EditLayoutToggle. */
function LeaveConversationButton({ onPress, busy }: { onPress: () => void; busy: boolean }) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="Leave conversation"
      accessibilityState={{ disabled: busy }}
      disabled={busy}
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.xs,
        paddingHorizontal: spacing.lg,
        paddingVertical: spacing.sm,
        borderRadius: radii.pill,
        opacity: pressed || busy ? 0.7 : 1,
      })}
    >
      <Feather name="log-out" size={typography.caption.fontSize} color={palette.inkSecondary} />
      <AppText variant="bodyBold" tone="secondary">
        Leave
      </AppText>
    </Pressable>
  );
}

function bubbleTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** No GET /users/{id} exists, so a titleless DM can't resolve the other
 * participant's name — fall back to a neutral, non-fabricated label. */
function conversationTitle(conversation: ConversationOut | null): string {
  if (conversation === null) return "Conversation";
  if (conversation.title) return conversation.title;
  return conversation.kind === "group" ? "Group" : "Direct message";
}

export default function ThreadScreen() {
  const palette = useTheme();
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();
  useSession();
  const owner = currentIdentity();
  const [, redraw] = useState(0);
  const [detail, setDetail] = useState<{ window: DurableWindow<MessageOut>; conversation: ConversationOut } | null>(null);
  const [leaving, setLeaving] = useState(false);
  const queryRef = useRef<DurableWindow<MessageOut> | null>(null);
  const window = useMemo(() => {
    const query: DurableWindow<MessageOut> = new DurableWindow({
      owner, selected: () => queryRef.current === query,
      changed: () => {
        if (query.state.denied) setDetail(current => current?.window === query ? null : current);
        redraw(value => value + 1);
      },
      pageSize: 50, refreshPages: 4,
      prepare: async operation => {
        const conversation = await getConversation(id, { operation });
        operation.assertCurrent();
        if (conversation.id !== id) throw new Error("Invalid conversation.");
        if (query.owns()) setDetail({ window: query, conversation });
      },
      page: async (cursor, operation) => {
        const rows = await listMessages(id, { ...cursor, limit: 50, operation });
        if (rows.some(row => row.conversation_id !== id)) throw new Error("Invalid conversation history.");
        return rows;
      },
      lookup: async (ids, operation) => {
        const rows = await getMessagesById(id, ids, { operation });
        if (rows.some(row => row.conversation_id !== id)) throw new Error("Invalid conversation history.");
        return rows;
      },
    });
    return query;
  }, [owner, id]);
  queryRef.current = window;
  const conversation = !window.state.denied && detail?.window === window ? detail.conversation : null;
  const { rows: messages, failed: loadFailed, denied, more, incomplete, loading, loadingMore } = window.state;
  const activation = window.activation;
  const load = () => window.ownsFocus(activation) ? window.refresh() : Promise.resolve();

  const handleLeave = useCallback(() => {
    if (!window.ownsFocus(activation)) return;
    confirmAction({
      title: "Leave this conversation?",
      message: "You'll stop receiving new messages here.",
      confirmLabel: "Leave", destructive: true,
      onConfirm: () => {
        if (!window.ownsFocus(activation)) return;
        setLeaving(true);
        void window.leave(operation => leaveConversation(id, { operation }), () => router.back())
          .finally(() => {
            if (!window.ownsFocus(activation)) return;
            setLeaving(false);
            if (window.state.failed) showApiError(new Error("Try again."), "Couldn't leave this conversation");
          });
      },
    });
  }, [window, id, router, activation]);

  useFocusEffect(useCallback(() => {
    const focus = window.activate();
    setLeaving(false);
    const unsubscribeEvent = chirpSocket.onEvent(event => {
      if (window.ownsFocus(focus) && isMessageEvent(event) && event.conversation_id === id) window.hint(event.message_id);
    });
    const unsubscribeStatus = chirpSocket.onStatus(status => { if (window.ownsFocus(focus) && status === "open") void window.refresh(); });
    void window.refresh();
    return () => { window.retire(); unsubscribeEvent(); unsubscribeStatus(); };
  }, [window, id]));

  return (
    <Screen
      title={conversationTitle(conversation)}
      onRefresh={load}
      subtitle={conversation?.kind === "group" ? "Group" : "Direct message"}
    >
      <View style={{ alignItems: "flex-end", marginBottom: spacing.sm }}>
        <LeaveConversationButton onPress={handleLeave} busy={leaving || denied} />
      </View>
      <View style={{ gap: spacing.sm }}>
        {loading ? <AppText variant="caption" tone="secondary">Refreshing conversation…</AppText> : null}
        {denied ? <EmptyState title="Conversation unavailable" message="We couldn't verify access to this conversation. Try again to check."
          actionLabel="Try again" onAction={() => void load()} /> : null}
        {incomplete && !denied ? <EmptyState title="Some updates may be missing"
          message="Refresh to check the latest messages. Older history may still need to be loaded."
          actionLabel="Refresh conversation" onAction={() => void load()} /> : null}
        {more && !denied ? <View style={{ gap: spacing.sm }}>
          <AppText variant="caption" tone="secondary">Showing a recent window. More history may include messages missed while offline.</AppText>
          <Button label={loadingMore ? "Loading older messages…" : "Load older messages"} variant="secondary"
            disabled={loading || loadingMore} onPress={() => { if (window.ownsFocus(activation)) void window.older(); }} />
        </View> : null}
        {loadFailed && !denied ? (
          <EmptyState
            title="Couldn't load this conversation"
            message="Check your connection and try again. This isn't a statement that nothing has been said."
            actionLabel="Try again"
            onAction={() => void load()}
          />
        ) : null}
        {messages.map((message) => (
          <View
            key={message.id}
            style={{
              alignSelf: "flex-start",
              maxWidth: "82%",
              gap: spacing.xs,
            }}
          >
            <View
              style={{
                flexDirection: "row",
                alignItems: "center",
                gap: spacing.xs,
                backgroundColor: palette.surfaceAlt,
                borderRadius: radii.card,
                // §7 tail corner: 6 (radii.sm) — every bubble reuses the
                // "theirs" corner since authorship can't be determined.
                borderBottomLeftRadius: radii.sm,
                paddingHorizontal: spacing.lg,
                paddingVertical: spacing.sm,
              }}
            >
              <Feather name="lock" size={typography.caption.fontSize} color={palette.inkFaint} />
              <AppText tone="secondary">Encrypted message</AppText>
            </View>
            <AppText variant="caption" tone="tertiary">
              {bubbleTime(message.created_at)}
            </AppText>
          </View>
        ))}

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
              // c336: this announced as a bare "button, dimmed". The icon carries the
              // meaning for sighted users and nothing at all for a screen reader, and
              // the caption explaining why it is disabled is a separate element the
              // button's focus never reaches. The hint reuses that visible wording
              // rather than inventing a second account of the same fact.
              accessibilityLabel="Send message"
              accessibilityHint="Sending unlocks with E2EE (milestone 4)"
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
              <Feather name="send" size={typography.headline.fontSize} color={palette.onAccent} />
            </View>
          </View>
          <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.xs }}>
            <Feather name="lock" size={typography.caption.fontSize} color={palette.inkFaint} />
            <AppText variant="caption" tone="tertiary">
              Sending unlocks with E2EE (milestone 4)
            </AppText>
          </View>
        </View>
      </View>
    </Screen>
  );
}
