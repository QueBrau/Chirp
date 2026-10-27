/**
 * Thread view per DESIGN §7: bubbles use the accent/surfaceAlt palette with a
 * 6/20 tail-corner radius.
 *
 * Direct messages (c451, E2EE M4) render src/messages/DmThread.tsx: plaintext bubbles
 * from the encrypted on-device store, mine vs theirs known, and a composer that is on
 * only when this phone's device is registered and approved. That component owns every
 * encryption claim on this screen and what to show when messaging is not ready.
 *
 * Groups are unchanged: real MessageOut carries only ciphertext_b64 (no plaintext) and
 * sender_device_id (no sender_user_id), group crypto does not exist yet, so every bubble
 * renders the same designed "encrypted" placeholder instead of faking a body or an
 * author. The pill composer + accent circular send stay disabled for groups.
 */

import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { e2ee } from "@/crypto/e2eeRuntime";
import { DmThread } from "@/messages/DmThread";
import { DmSecurityPill } from "@/messages/DmSecurityPill";
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
  const { user } = useSession();
  const owner = currentIdentity();
  const [, redraw] = useState(0);
  const [detail, setDetail] = useState<{ window: DurableWindow<MessageOut>; conversation: ConversationOut } | null>(null);
  const [leaving, setLeaving] = useState(false);
  const [cleanupPending, setCleanupPending] = useState<DurableWindow<MessageOut> | null>(null);
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
  // c451: a DM is read from the encrypted on-device store, not from this v1 window.
  const isDm = conversation?.kind === "dm";
  const dmPeerId = isDm
    ? (conversation.members ?? []).find((member) => member.left_at === null && member.user_id !== user?.id)?.user_id ?? null
    : null;
  const activation = window.activation;
  const load = () => window.ownsFocus(activation) ? window.refresh() : Promise.resolve();

  useEffect(() => {
    let current = true;
    if (user === null) return;
    void e2ee.conversationCleanupStatus(user.id, id).then(phase => {
      if (current && window.owns() && (phase === "requested" || phase === "confirmed")) setCleanupPending(window);
    }).catch(() => { if (current && window.owns()) setCleanupPending(window); });
    return () => { current = false; };
  }, [window, id, user?.id]);

  const performLeave = useCallback((resumeOnly = false) => {
    if (!window.ownsFocus(activation) || user === null) return;
    const userId = user.id;
    setLeaving(true);
    setCleanupPending(window);
    void window.leave(async operation => {
      operation.assertCurrent();
      await e2ee.leaveConversation(userId, id, () => {
        operation.assertCurrent();
        return leaveConversation(id, { operation });
      }, resumeOnly);
    }, () => router.back()).finally(() => {
      if (!window.ownsFocus(activation)) return;
      setLeaving(false);
      if (window.state.failed) showApiError(new Error("Try again."), "Couldn't finish leaving this conversation");
    });
  }, [window, activation, user?.id, id, router]);

  const handleLeave = useCallback(() => {
    if (!window.ownsFocus(activation)) return;
    confirmAction({
      title: "Leave this conversation?",
      message: "You'll stop receiving new messages here.",
      confirmLabel: "Leave", destructive: true,
      onConfirm: () => performLeave(false),
    });
  }, [window, activation, performLeave]);

  useFocusEffect(useCallback(() => {
    const focus = window.activate();
    setLeaving(false);
    const unsubscribeEvent = chirpSocket.onEvent(event => {
      // A v2 (encrypted DM) hint is for the DM thread; the v1 window would only look for a row it never has.
      if (window.ownsFocus(focus) && isMessageEvent(event) && event.conversation_id === id && event.envelope_version === undefined) window.hint(event.message_id);
    });
    const unsubscribeStatus = chirpSocket.onStatus(status => { if (window.ownsFocus(focus) && status === "open") void window.refresh(); });
    void window.refresh();
    return () => { window.retire(); unsubscribeEvent(); unsubscribeStatus(); };
  }, [window, id]));

  return (
    <Screen
      // c451: a DM thread owns the screen's height (inverted list + pinned composer), so it
      // does not live inside Screen's ScrollView. Groups keep the scrolling layout.
      scroll={!isDm}
      title={conversationTitle(conversation)}
      onRefresh={load}
      subtitle={conversation?.kind === "group" ? "Group" : "Direct message"}
    >
      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginBottom: spacing.sm }}>
        {/* c463: a DM's way into the safety number, with a small Verified mark once marked. */}
        {isDm && !denied ? <DmSecurityPill conversationId={id} peerUserId={dmPeerId} peerName={conversation?.title ?? null} /> : <View />}
        <LeaveConversationButton onPress={handleLeave} busy={leaving || denied} />
      </View>
      <View style={isDm ? { flex: 1, gap: spacing.sm } : { gap: spacing.sm }}>
        {cleanupPending === window ? <EmptyState title="Finish removing local messages"
          message="This conversation is hidden while your phone finishes removing its saved messages. Retry to complete the request."
          actionLabel={leaving ? "Removing..." : "Retry removal"} onAction={() => performLeave(true)} /> : null}
        {loading && !isDm ? <AppText variant="caption" tone="secondary">Refreshing conversation…</AppText> : null}
        {denied ? <EmptyState title="Conversation unavailable" message="We couldn't verify access to this conversation. Try again to check."
          actionLabel="Try again" onAction={() => void load()} /> : null}
        {incomplete && !denied && !isDm ? <EmptyState title="Some updates may be missing"
          message="Refresh to check the latest messages. Older history may still need to be loaded."
          actionLabel="Refresh conversation" onAction={() => void load()} /> : null}
        {more && !denied && !isDm ? <View style={{ gap: spacing.sm }}>
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
        {/* c451: a DM is its own component (plaintext from the encrypted store). */}
        {cleanupPending !== window && (isDm && !denied ? (
          <DmThread conversationId={id} peerUserId={dmPeerId} myUserId={user?.id ?? null} peerName={conversation?.title ?? null} />
        ) : null)}

        {/* Groups only: the v1 placeholder bubbles and the disabled composer, unchanged. */}
        {conversation?.kind === "group" ? messages.map((message) => (
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
        )) : null}

        {/* Groups only. Composer per §7: pill input + accent circular send — visually
            present, disabled until group crypto lands (c451 turned DMs on; groups are
            a later design, E2EE-DESIGN.md section 9). */}
        {conversation?.kind === "group" ? (
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
              accessibilityHint="Group messages aren't available yet."
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
            <Feather name="info" size={typography.caption.fontSize} color={palette.inkFaint} />
            <AppText variant="caption" tone="tertiary">
              Group messages aren't available yet.
            </AppText>
          </View>
        </View>
        ) : null}
      </View>
    </Screen>
  );
}
