/**
 * Thread view per DESIGN §7: bubbles use the accent/surfaceAlt palette with a
 * 6/20 tail-corner radius. Real MessageOut carries only ciphertext_b64 (no
 * plaintext) and sender_device_id (no sender_user_id), so this build can
 * neither decrypt a body nor tell "mine" from "theirs" — every bubble renders
 * the same designed "encrypted" placeholder instead of faking either. Pill
 * composer + accent circular send stay disabled until the libsignal pipeline
 * lands (milestone 4).
 */

import { useLocalSearchParams } from "expo-router";
import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { TextInput, View } from "react-native";

import {
  listConversations,
  listMessages,
  type ConversationOut,
  type MessageOut,
} from "@/api/messages";
import { AppText, EmptyState, Screen } from "@/components";
import { chirpSocket, isMessageEvent } from "@/realtime/socket";
import { metrics, radii, spacing, typography, useTheme } from "@/theme";

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
  const { id } = useLocalSearchParams<{ id: string }>();
  const [conversation, setConversation] = useState<ConversationOut | null>(null);
  const [messages, setMessages] = useState<MessageOut[]>([]);
  /** The history fetch failed. Distinct from a genuinely empty thread (c317). */
  const [loadFailed, setLoadFailed] = useState(false);
  // True once this screen has observed (or started inside) an "open" socket,
  // so a LATER "open" is a real reconnect and not the first connection
  // completing.
  const wasOpenRef = useRef(false);

  /**
   * c317: this used to be `load().catch(() => setMessages([]))`, justified as
   * "matches the repo pattern elsewhere in this stack" — the same sentence that
   * justified the bug in chapter/members.tsx before c299 removed it. The pattern
   * was never the convention; it was the defect, repeated.
   *
   * An empty thread and an unreachable server are not the same claim. This screen
   * has no empty-state copy at all, so a failed load rendered zero bubbles and
   * nothing else — which in a DM reads as the other person never having written
   * anything. That is a statement about a person, made out of a dropped request.
   *
   * The catch lives INSIDE this callback rather than at the call site, for the
   * reason c299 found on messages/index.tsx: the retry action below invokes load()
   * directly, and a catch attached only to the mount effect would leave a failed
   * RETRY unhandled — silently, and precisely when the user is already failing.
   */
  const load = useCallback(async () => {
    setLoadFailed(false);
    try {
      const conversations = await listConversations();
      setConversation(conversations.find((c) => c.id === id) ?? null);
      const history = await listMessages(id);
      setMessages(history);
    } catch {
      setLoadFailed(true);
    }
  }, [id]);

  useEffect(() => {
    // NOT unconditionally false. onStatus() only adds a listener — it never
    // replays the CURRENT status to a new subscriber (see socket.ts) — and
    // since SessionProvider connects at sign-in, the socket is almost always
    // already "open" by the time a user taps into a thread minutes later.
    // Starting this false in that case meant the very next status event this
    // screen ever saw (the first REAL reconnect after a real outage) was
    // wrongly treated as "the initial connection completing" and skipped its
    // catch-up fetch — exactly the outage it existed to catch up on. Reading
    // the actual current status makes "was it already open when I mounted"
    // the question, not "have I personally seen an open event yet".
    wasOpenRef.current = chirpSocket.getStatus() === "open";

    void load();

    // c63: live-append messages published for THIS conversation while the
    // screen is open. Deduped by id — the socket can genuinely double-deliver
    // (e.g. a reconnect's catch-up fetch below racing a not-yet-processed
    // live event for the same message).
    const unsubEvent = chirpSocket.onEvent((event) => {
      if (!isMessageEvent(event) || event.conversation_id !== id) return;
      setMessages((current) => {
        if (current.some((message) => message.id === event.message_id)) return current;
        return [
          ...current,
          {
            id: event.message_id,
            conversation_id: event.conversation_id,
            sender_device_id: event.sender_device_id ?? "",
            ciphertext_b64: event.ciphertext ?? "",
            // routers/messages.py's publish never actually sets this field on
            // the wire (checked the event dict directly), so the optional type
            // on MessageSocketEvent is aspirational today — "signal" is the
            // real-content case; sender_key_distribution is the protocol
            // handshake type, not a reasonable default for an unknown message.
            message_type: event.message_type ?? "signal",
            created_at: event.created_at ?? new Date().toISOString(),
          },
        ];
      });
    });

    // c63: pub/sub drops anything published while the socket was down (proven
    // in c21's suite) — a reconnect has no memory of what it missed. Refetch
    // on every "open" AFTER the first one, which is the signal that a real
    // disconnect just ended rather than the initial connection completing.
    const unsubStatus = chirpSocket.onStatus((status) => {
      if (status !== "open") return;
      if (!wasOpenRef.current) {
        wasOpenRef.current = true;
        return;
      }
      listMessages(id)
        .then(setMessages)
        .catch(() => {
          // Fail soft: the live-append path above still works going forward,
          // this only means whatever was missed during the outage stays missed.
        });
    });

    return () => {
      unsubEvent();
      unsubStatus();
    };
  }, [id, load]);

  return (
    <Screen
      title={conversationTitle(conversation)}
      subtitle={conversation?.kind === "group" ? "Group" : "Direct message"}
    >
      <View style={{ gap: spacing.sm }}>
        {loadFailed ? (
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
