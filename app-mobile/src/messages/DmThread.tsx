/**
 * The body of a direct-message thread (board c451, E2EE milestone M4): bubbles from the
 * encrypted on-device store, and a composer that is on only when sending is real.
 *
 * TWO COMPONENTS, ON PURPOSE. `ReadyThread` is the only place that may claim encryption,
 * and it is rendered only when `useE2ee().status === "ready"`: this phone's device is
 * registered, approved, and backed by the native core. Everything else (a build without the
 * native module, a phone still waiting for approval, a failed start) gets `NotReadyThread`,
 * which says what is true and offers no composer. `npm run verify:e2ee-claims` reads this
 * file and fails if an encryption claim appears anywhere but `ReadyThread`, or if
 * `ReadyThread` could be reached without the ready check.
 *
 * Bubbles follow DESIGN.md section 7: mine = accent fill with onAccent text and a 6px
 * bottom-right corner; theirs = surfaceAlt with ink text and a 6px bottom-left corner.
 * A hard stop (a changed identity, an unapproved device) pauses SENDING with a banner and two
 * ways out (c463, E2EE M5): look at the safety number, or accept the new code after a
 * confirmation that says what accepting means. Messages already received stay readable. The
 * same banner covers this account's OWN phones, with copy that fits. A phone still waiting for
 * approval shows its comparison code (the same digits the approving phone shows) and a Reset
 * security way out; neither claims anything about encryption.
 *
 * LAYOUT. The thread screen renders this inside a non-scrolling Screen, so this component
 * owns the whole remaining height: an INVERTED FlatList (newest message at the bottom, the
 * thread opens at the newest message and stays pinned there while the reader is at the
 * bottom, with no scroll bookkeeping) and a composer pinned under it that rides above the
 * keyboard. Without a scroll view there is no clearance for the floating tab bar, so the
 * composer block adds `useOverlayClearance` itself, except while the keyboard covers the bar.
 *
 * A SERVER WITHOUT v2. Until the backend is redeployed with the /v2 routes, a build that
 * points at it gets `unavailable` / `server_not_ready`: a quiet "coming soon" notice (not a
 * warning, not an error, no retry button) and the same disabled composer.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { FlatList, Keyboard, KeyboardAvoidingView, Platform, Pressable, TextInput, View } from "react-native";

import { AppText, Button } from "@/components";
import { e2ee } from "@/crypto/e2eeRuntime";
import { useE2ee, useEncryptedDm, useOwnComparisonCode } from "@/crypto/E2eeProvider";
import { groupComparisonCode } from "@/crypto/e2eeTrust";
import {
  ACCOUNT_DELETING,
  DEVICE_REVOKED,
  E2eeSendError,
  E2eeTrustError,
  MAX_DM_BODY_CHARS,
  SERVER_NOT_READY,
  type E2eeStatus,
  type TrustBlock,
} from "@/crypto/e2eeTypes";
import type { ThreadItem, ThreadMessage } from "@/crypto/e2eeThread";
import { confirmAction } from "@/lib/alert";
import { metrics, radii, spacing, typography, useOverlayClearance, useTheme } from "@/theme";

interface DmThreadProps {
  conversationId: string;
  /** The other member, or null if they have left. */
  peerUserId: string | null;
  myUserId: string | null;
  /** The other person's name when the conversation carries one; the copy says "they" otherwise. */
  peerName?: string | null;
}

export function DmThread(props: DmThreadProps) {
  const state = useE2ee();
  if (state.status === "ready") return <ReadyThread {...props} />;
  return <NotReadyThread status={state.status} reason={state.reason} />;
}

/** Whether the on-screen keyboard is up (the floating tab bar is hidden behind it then). */
function useKeyboardVisible(): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const ios = Platform.OS === "ios";
    const show = Keyboard.addListener(ios ? "keyboardWillShow" : "keyboardDidShow", () => setVisible(true));
    const hide = Keyboard.addListener(ios ? "keyboardWillHide" : "keyboardDidHide", () => setVisible(false));
    return () => {
      show.remove();
      hide.remove();
    };
  }, []);
  return visible;
}

/** The space under the composer: clear the floating tab bar, unless the keyboard covers it. */
function useComposerBottomPadding(): number {
  const clearance = useOverlayClearance(false, true);
  return useKeyboardVisible() ? spacing.sm : clearance;
}

// ---------------------------------------------------------------------------
// Ready: a real encrypted thread
// ---------------------------------------------------------------------------

function ReadyThread({ conversationId, peerUserId, myUserId, peerName }: DmThreadProps) {
  const palette = useTheme();
  const router = useRouter();
  const dm = useEncryptedDm(conversationId, peerUserId, myUserId);
  const bottomPadding = useComposerBottomPadding();
  const list = useRef<FlatList<ThreadItem>>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);

  const block = dm.trust.block;
  const canSend = peerUserId !== null && block === null;
  const hasText = text.trim().length > 0;
  // Newest first, because the list is inverted: index 0 is the bottom of the screen.
  const newestFirst = useMemo(() => [...dm.items].reverse(), [dm.items]);

  const submit = async () => {
    const body = text.trim();
    if (body.length === 0 || busy || !canSend) return;
    setBusy(true);
    setProblem(null);
    try {
      await dm.send(body);
      setText("");
      // Your own message always brings you back to the bottom, even from scrolled-up history.
      list.current?.scrollToOffset({ offset: 0, animated: true });
    } catch (error) {
      setProblem(sendProblem(error));
    } finally {
      setBusy(false);
    }
  };

  const viewSafetyNumber = () => {
    if (peerUserId === null) return;
    router.push({
      pathname: "/messages/safety-number",
      params: { peer: peerUserId, conversation: conversationId, ...(peerName ? { name: peerName } : {}) },
    });
  };

  const acceptNewCode = () => {
    if (block === null || accepting) return;
    const own = block.userId === myUserId;
    confirmAction({
      title: own ? "Accept the phones on your account?" : acceptTitle(peerName),
      message: own
        ? "Only accept if you recognize every phone signed in to your account. Messages you send afterwards will be readable on all of them."
        : "Only accept if you have checked the safety number with them in person, or you know why it changed. Messages you send afterwards will be readable on every phone they have, including one you haven't confirmed.",
      confirmLabel: own ? "Accept change" : "Accept new code",
      onConfirm: () => {
        setAccepting(true);
        setProblem(null);
        dm.acceptNewCode()
          .catch((error: unknown) => setProblem(trustProblem(error, peerName)))
          .finally(() => setAccepting(false));
      },
    });
  };

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <FlatList
        ref={list}
        style={{ flex: 1 }}
        inverted
        data={newestFirst}
        keyExtractor={(item) => item.key}
        renderItem={({ item }) => <ThreadRow item={item} onRetry={(id) => void dm.retry(id)} />}
        contentContainerStyle={{ gap: spacing.sm, paddingVertical: spacing.sm }}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="interactive"
        showsVerticalScrollIndicator={false}
        // An inverted list draws its footer at the TOP: the oldest end of the history.
        ListFooterComponent={
          dm.hasOlder ? <Button label="Load earlier messages" variant="secondary" onPress={() => void dm.loadOlder()} /> : null
        }
      />

      <View style={{ gap: spacing.sm, paddingTop: spacing.sm, paddingBottom: bottomPadding }}>
        {dm.loading ? <AppText variant="caption" tone="secondary">Loading messages…</AppText> : null}
        {!dm.loading && dm.items.length === 0 ? (
          <AppText variant="caption" tone="secondary">No messages yet. Say hello.</AppText>
        ) : null}
        {block !== null ? (
          <Banner
            text={blockedCopy(block, myUserId, peerName)}
            detail={blockedDetail(block, myUserId)}
            actions={[
              ...(block.userId === myUserId
                ? []
                : [{ label: "View safety number", variant: "neutral" as const, onPress: viewSafetyNumber }]),
              {
                label: accepting ? "Accepting…" : block.userId === myUserId ? "Accept change" : "Accept new code",
                variant: "neutral" as const,
                disabled: accepting,
                onPress: acceptNewCode,
              },
            ]}
          />
        ) : null}
        {peerUserId === null ? <Banner text="The other person has left this conversation." /> : null}
        <Composer
          enabled={canSend}
          busy={busy}
          hasText={hasText}
          value={text}
          onChange={setText}
          onSubmit={() => void submit()}
          disabledReason={block !== null ? "Sending is paused" : "Sending isn't available here"}
        />
        {problem !== null ? <AppText variant="caption" tone="danger">{problem}</AppText> : null}
        <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.xs }}>
          <Feather name="lock" size={typography.caption.fontSize} color={palette.inkFaint} />
          <AppText variant="caption" tone="tertiary">End-to-end encrypted</AppText>
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}

// ---------------------------------------------------------------------------
// Not ready: say what is true, offer no composer
// ---------------------------------------------------------------------------

function NotReadyThread({ status, reason }: { status: E2eeStatus; reason: string | null }) {
  const [checking, setChecking] = useState(false);
  const bottomPadding = useComposerBottomPadding();
  const router = useRouter();
  const ownCode = useOwnComparisonCode();
  const recheck = async () => {
    setChecking(true);
    try {
      await e2ee.recheck();
    } finally {
      setChecking(false);
    }
  };
  // Two `unavailable` states are not failures and have nothing to retry: the server has no
  // direct-message API yet, and the account is being deleted. They get a quiet notice.
  const quiet = status === "unavailable" && (reason === SERVER_NOT_READY || reason === ACCOUNT_DELETING);
  const copy = notReadyCopy(status, reason);
  // Development builds also show the fixed failure code, so a failed start can be diagnosed
  // from the screen. A release build never shows it, and a quiet state is not a failure.
  const text = __DEV__ && reason !== null && !quiet ? `${copy.text} (${reason})` : copy.text;
  return (
    <View style={{ flex: 1, justifyContent: "space-between" }}>
      {quiet ? (
        <Notice text={text} />
      ) : status === "pending_approval" ? (
        <PendingApproval
          code={ownCode}
          checking={checking}
          onCheck={() => void recheck()}
          onReset={() => router.push("/messages/reset-security")}
        />
      ) : (
        <Banner
          text={text}
          actions={
            copy.action === null
              ? []
              : [{ label: checking ? "Checking…" : copy.action, variant: "neutral", disabled: checking, onPress: () => void recheck() }]
          }
        />
      )}
      <View style={{ paddingTop: spacing.lg, paddingBottom: bottomPadding }}>
        <Composer enabled={false} busy={false} hasText={false} value="" onChange={() => undefined} onSubmit={() => undefined} disabledReason={copy.text} />
      </View>
    </View>
  );
}

function notReadyCopy(status: E2eeStatus, reason: string | null): { text: string; action: string | null } {
  switch (status) {
    case "starting":
    case "inactive":
      return { text: "Getting this phone ready…", action: null };
    case "pending_approval":
      return { text: "This phone is waiting for approval from your other phone before it can send or read messages.", action: "Check again" };
    case "error":
      // c463: Reset security (or "Not me") removed this phone from the account. That is final, so
      // there is no retry to offer: a fresh install is how a phone comes back.
      if (reason === DEVICE_REVOKED) {
        return {
          text: "This phone was removed from your account, so it can't send or read messages. To use messages on it again, delete Chirp and install it again.",
          action: null,
        };
      }
      return { text: "Couldn't get messaging ready. Check your connection and try again.", action: "Try again" };
    case "unavailable":
      // The server this build talks to has no direct-message API yet. Nothing is wrong.
      if (reason === SERVER_NOT_READY) return { text: "Direct messages are coming soon.", action: null };
      // c436: an account deletion is running; the server refuses every write until it is done.
      if (reason === ACCOUNT_DELETING) return { text: "Messaging is unavailable while an account deletion is in progress.", action: null };
      return Platform.OS === "ios"
        ? { text: "Messaging needs the latest version of the app.", action: null }
        : { text: "Messaging is available in the iPhone app for now.", action: null };
    case "ready":
      return { text: "", action: null };
  }
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

/** "Jose's", or "Their" when the conversation carries no name. */
function possessive(peerName: string | null | undefined): string {
  const name = peerName?.trim();
  if (!name) return "Their";
  return name.endsWith("s") ? `${name}'` : `${name}'s`;
}

function acceptTitle(peerName: string | null | undefined): string {
  const name = peerName?.trim();
  return name ? `Accept ${possessive(name)} new security code?` : "Accept their new security code?";
}

/** The banner's headline: what happened, in the words a person would use. */
function blockedCopy(block: TrustBlock, myUserId: string | null, peerName: string | null | undefined): string {
  if (block.userId === myUserId) {
    return "The phones on your account changed in a way this phone can't confirm.";
  }
  const name = peerName?.trim();
  if (block.reason === "identity_changed") return `${possessive(peerName)} security code changed.`;
  return name ? `${name} added a device that wasn't approved.` : "They added a device that wasn't approved.";
}

/** The line under the headline: what it means for sending and reading. */
function blockedDetail(block: TrustBlock, myUserId: string | null): string {
  if (block.userId === myUserId) {
    return "Sending is paused. You can still read messages that arrive.";
  }
  return block.reason === "identity_changed"
    ? "Sending is paused until you accept the new code. You can still read what they send."
    : "This also happens when someone resets security or reinstalls Chirp. Sending is paused until you accept it. You can still read what they send.";
}

/** Plain words for why accepting did not go through. */
function trustProblem(error: unknown, peerName: string | null | undefined): string {
  if (error instanceof E2eeTrustError) {
    switch (error.code) {
      case "changed_again":
        return `${possessive(peerName)} code changed again while you were deciding. Look at the new safety number before accepting.`;
      case "network":
        return "Couldn't reach the server. Check your connection and try again.";
      case "account_deletion":
        return "That can't be changed while an account in this conversation is being deleted.";
      case "peer_no_devices":
        return "They don't have a phone set up for messages right now.";
      default:
        return "Couldn't accept the new code. Try again.";
    }
  }
  return "Couldn't accept the new code. Try again.";
}

function sendProblem(error: unknown): string {
  if (error instanceof E2eeSendError) {
    switch (error.code) {
      case "offline":
        return "You're offline, so the message wasn't sent.";
      case "peer_no_devices":
        return "They haven't set up messaging on their phone yet.";
      case "not_allowed":
        return "You can't message this person.";
      case "account_deletion":
        return "Messages can't be sent while an account in this conversation is being deleted.";
      case "server_busy":
        return "Messaging is busy right now. Try again in a moment.";
      case "device_unusable":
        return "This phone can't send right now.";
      case "invalid_body":
        return `Messages can't be empty or longer than ${MAX_DM_BODY_CHARS.toLocaleString()} characters.`;
      case "blocked":
        return "Sending is paused.";
      case "not_ready":
        return "This phone isn't ready yet. Try again in a moment.";
      case "rejected":
        return "Couldn't send. Try again.";
    }
  }
  return "Couldn't send. Try again.";
}

function bubbleTime(atMs: number): string {
  return new Date(atMs).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function ThreadRow({ item, onRetry }: { item: ThreadItem; onRetry: (clientMessageId: string) => void }) {
  if (item.kind === "undecryptable") return <UndecryptableRow />;
  return <Bubble item={item} onRetry={onRetry} />;
}

/** A message arrived that this phone could not read. Says nothing about its content. */
function UndecryptableRow() {
  const palette = useTheme();
  return (
    <View
      accessible
      accessibilityLabel="A message that couldn't be decrypted"
      style={{
        alignSelf: "flex-start",
        maxWidth: "82%",
        borderRadius: radii.card,
        borderBottomLeftRadius: radii.sm,
        borderWidth: 1,
        borderColor: palette.border,
        paddingHorizontal: spacing.lg,
        paddingVertical: spacing.sm,
      }}
    >
      <AppText variant="caption" tone="secondary">Couldn't decrypt this message</AppText>
    </View>
  );
}

function Bubble({ item, onRetry }: { item: ThreadMessage; onRetry: (clientMessageId: string) => void }) {
  const palette = useTheme();
  const failed = item.delivery === "failed";
  const caption = failed ? "Not sent. Tap to try again." : item.delivery === "sending" ? "Sending…" : bubbleTime(item.atMs);
  return (
    <View style={{ alignSelf: item.mine ? "flex-end" : "flex-start", maxWidth: "82%", gap: spacing.xs }}>
      <Pressable
        disabled={!failed}
        accessibilityRole={failed ? "button" : undefined}
        accessibilityHint={failed ? "Tries to send this message again" : undefined}
        onPress={() => onRetry(item.clientMessageId)}
      >
        <View
          style={{
            backgroundColor: item.mine ? palette.accent : palette.surfaceAlt,
            borderRadius: radii.card,
            // DESIGN section 7: the corner nearest the sender's edge is 6.
            ...(item.mine ? { borderBottomRightRadius: radii.sm } : { borderBottomLeftRadius: radii.sm }),
            paddingHorizontal: spacing.lg,
            paddingVertical: spacing.sm,
          }}
        >
          <AppText tone={item.mine ? "onAccent" : "primary"} selectable>{item.body}</AppText>
        </View>
      </Pressable>
      <AppText
        variant="caption"
        tone={failed ? "danger" : "tertiary"}
        style={{ alignSelf: item.mine ? "flex-end" : "flex-start" }}
      >
        {caption}
      </AppText>
    </View>
  );
}

/** A quiet, neutral line for something that is simply not here yet (not a warning, not an error). */
function Notice({ text }: { text: string }) {
  const palette = useTheme();
  return (
    <View
      accessibilityRole="text"
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.sm,
        backgroundColor: palette.surfaceAlt,
        borderRadius: radii.input,
        padding: spacing.md,
      }}
    >
      <Feather name="info" size={typography.body.fontSize} color={palette.inkSecondary} />
      <AppText variant="caption" tone="secondary" style={{ flex: 1 }}>{text}</AppText>
    </View>
  );
}

interface BannerAction {
  label: string;
  onPress: () => void;
  variant?: "neutral" | "ghost";
  disabled?: boolean;
}

function Banner({ text, detail, actions = [] }: { text: string; detail?: string; actions?: BannerAction[] }) {
  const palette = useTheme();
  return (
    <View
      accessibilityRole="alert"
      style={{
        backgroundColor: palette.warningSoft,
        borderRadius: radii.input,
        padding: spacing.md,
        gap: spacing.sm,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "flex-start", gap: spacing.sm }}>
        <Feather name="alert-circle" size={typography.body.fontSize} color={palette.ink} />
        <View style={{ flex: 1, gap: spacing.xs }}>
          <AppText variant="bodyBold">{text}</AppText>
          {detail !== undefined ? <AppText variant="caption" tone="secondary">{detail}</AppText> : null}
        </View>
      </View>
      {actions.map((action) => (
        <Button
          key={action.label}
          label={action.label}
          variant={action.variant ?? "neutral"}
          disabled={action.disabled === true}
          onPress={action.onPress}
        />
      ))}
    </View>
  );
}

/**
 * A phone that is registered but not yet approved (c463). It shows the code the approving phone
 * shows for it, so the person can check the two screens against each other, says where to
 * approve it, and offers Reset security for when there is no other phone to approve from.
 */
function PendingApproval({
  code,
  checking,
  onCheck,
  onReset,
}: {
  code: string | null;
  checking: boolean;
  onCheck: () => void;
  onReset: () => void;
}) {
  const palette = useTheme();
  return (
    <View
      accessibilityRole="alert"
      style={{
        backgroundColor: palette.warningSoft,
        borderRadius: radii.input,
        padding: spacing.md,
        gap: spacing.md,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "flex-start", gap: spacing.sm }}>
        <Feather name="smartphone" size={typography.body.fontSize} color={palette.ink} />
        <View style={{ flex: 1, gap: spacing.xs }}>
          <AppText variant="bodyBold">This phone is waiting for approval.</AppText>
          <AppText variant="caption" tone="secondary">Open Chirp on your other phone to approve this one.</AppText>
        </View>
      </View>
      {code !== null ? (
        <View
          accessible
          accessibilityLabel={`Code for this phone: ${code.split("").join(" ")}`}
          style={{ backgroundColor: palette.surface, borderRadius: radii.input, padding: spacing.md, gap: spacing.xs }}
        >
          <AppText variant="micro" tone="secondary">Code for this phone</AppText>
          <AppText variant="display" style={{ fontVariant: ["tabular-nums"] }}>{groupComparisonCode(code)}</AppText>
          <AppText variant="caption" tone="secondary">Your other phone shows the same code. Approve only if they match.</AppText>
        </View>
      ) : null}
      <Button label={checking ? "Checking…" : "Check again"} variant="neutral" disabled={checking} onPress={onCheck} />
      <Button label="Reset security" variant="ghost" onPress={onReset} />
    </View>
  );
}

function Composer({
  enabled,
  busy,
  hasText,
  value,
  onChange,
  onSubmit,
  disabledReason,
}: {
  enabled: boolean;
  busy: boolean;
  hasText: boolean;
  value: string;
  onChange: (text: string) => void;
  onSubmit: () => void;
  disabledReason: string;
}) {
  const palette = useTheme();
  const canPress = enabled && hasText && !busy;
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
      <TextInput
        editable={enabled}
        value={value}
        onChangeText={onChange}
        onSubmitEditing={onSubmit}
        returnKeyType="send"
        blurOnSubmit={false}
        maxLength={MAX_DM_BODY_CHARS}
        placeholder="Message"
        placeholderTextColor={palette.inkFaint}
        accessibilityLabel="Message"
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
      <Pressable
        onPress={onSubmit}
        disabled={!canPress}
        accessibilityRole="button"
        accessibilityLabel="Send message"
        accessibilityHint={enabled ? undefined : disabledReason}
        accessibilityState={{ disabled: !canPress, busy }}
        style={{
          width: metrics.buttonHeight,
          height: metrics.buttonHeight,
          borderRadius: radii.pill,
          backgroundColor: palette.accent,
          alignItems: "center",
          justifyContent: "center",
          opacity: canPress ? 1 : 0.4,
        }}
      >
        <Feather name="send" size={typography.headline.fontSize} color={palette.onAccent} />
      </Pressable>
    </View>
  );
}
