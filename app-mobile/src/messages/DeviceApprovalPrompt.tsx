/**
 * "A new phone signed in to your account" (board c463, E2EE milestone M5, design section 8):
 * the sheet an APPROVED phone shows when another phone of the same account is waiting to be
 * approved. It shows the short comparison code of the new phone, derived from the very identity
 * this phone is about to sign, so the person can hold the two phones side by side and compare.
 *
 *   Approve   this phone signs the new phone and the server records it (engine.approvePending).
 *   Not me    the new phone is removed from the account (engine.denyPending), and the sheet
 *             then says to change the account password: a phone nobody here recognises means
 *             someone else has the password.
 *   Decide later  hides the prompt until the app returns to the foreground; nothing changes.
 *
 * Mounted once, inside the theme, by the root layout. It renders nothing unless this phone is
 * ready and a pending phone exists, and its copy makes no claim about encryption.
 */

import { useEffect, useState } from "react";
import { Modal, Pressable, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { AppText, Button } from "@/components";
import { usePendingApprovals } from "@/crypto/E2eeProvider";
import { e2ee } from "@/crypto/e2eeRuntime";
import { groupComparisonCode } from "@/crypto/e2eeTrust";
import { E2eeTrustError, type PendingDevice, type TrustFailureCode } from "@/crypto/e2eeTypes";
import { light, radii, spacing, useTheme, withAlpha } from "@/theme";

function problemCopy(code: TrustFailureCode): string {
  switch (code) {
    case "network":
      return "Couldn't reach the server. Check your connection and try again.";
    case "gone":
      return "That phone is no longer waiting. Nothing more to do.";
    case "account_deletion":
      return "That can't be changed while this account is being deleted.";
    case "not_ready":
      return "This phone isn't ready to approve another one.";
    default:
      return "Couldn't finish. Try again.";
  }
}

function signedInAt(createdAt: string): string {
  const time = new Date(createdAt);
  return Number.isNaN(time.getTime())
    ? "Signed in just now"
    : `Signed in at ${time.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

export function DeviceApprovalPrompt() {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const pending = usePendingApprovals();
  const first: PendingDevice | undefined = pending[0];
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [problem, setProblem] = useState<TrustFailureCode | null>(null);
  // After "Not me" the pending phone is gone from the list, but the person still needs the advice.
  const [removed, setRemoved] = useState(false);

  // A different phone is now first (or none): clear what belonged to the last one.
  const firstId = first?.deviceId ?? null;
  useEffect(() => {
    setProblem(null);
  }, [firstId]);

  if (first === undefined && !removed) return null;

  const approve = async () => {
    if (first === undefined || busy !== null) return;
    setBusy("approve");
    setProblem(null);
    try {
      await e2ee.approvePending(first.deviceId);
    } catch (error) {
      setProblem(error instanceof E2eeTrustError ? error.code : "rejected");
    } finally {
      setBusy(null);
    }
  };

  const deny = async () => {
    if (first === undefined || busy !== null) return;
    setBusy("deny");
    setProblem(null);
    try {
      await e2ee.denyPending(first.deviceId);
      setRemoved(true);
    } catch (error) {
      setProblem(error instanceof E2eeTrustError ? error.code : "rejected");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Modal visible transparent animationType="slide" onRequestClose={() => (removed ? setRemoved(false) : e2ee.snoozePending(first?.deviceId ?? ""))}>
      <View style={{ flex: 1, backgroundColor: withAlpha(light.ink, 0.4), justifyContent: "flex-end" }}>
        <View
          style={{
            backgroundColor: palette.surface,
            borderTopLeftRadius: radii.card,
            borderTopRightRadius: radii.card,
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.lg,
            paddingBottom: insets.bottom + spacing.lg,
            gap: spacing.lg,
          }}
        >
          <View
            style={{ alignSelf: "center", width: 40, height: 4, borderRadius: radii.pill, backgroundColor: palette.border }}
          />
          {removed ? (
            <>
              <AppText variant="title">That phone was removed</AppText>
              <AppText variant="body" tone="secondary">
                If you didn't sign in on a new phone, someone else may have your password. Change your account password now.
              </AppText>
              <Button label="Done" onPress={() => setRemoved(false)} />
            </>
          ) : first !== undefined ? (
            <>
              <AppText variant="title">A new phone signed in to your account</AppText>
              <AppText variant="body" tone="secondary">
                If this is you, check that the code below matches the one on your new phone, then approve it.
              </AppText>
              <View
                accessible
                accessibilityLabel={`Code for the new phone: ${first.code.split("").join(" ")}`}
                style={{ backgroundColor: palette.surfaceAlt, borderRadius: radii.input, padding: spacing.lg, gap: spacing.xs }}
              >
                <AppText variant="micro" tone="secondary">Code for the new phone</AppText>
                <AppText variant="display" style={{ fontVariant: ["tabular-nums"] }}>{groupComparisonCode(first.code)}</AppText>
                <AppText variant="caption" tone="secondary">{`${first.label ?? "A new phone"}. ${signedInAt(first.createdAt)}.`}</AppText>
              </View>
              {pending.length > 1 ? (
                <AppText variant="caption" tone="secondary">{`${pending.length - 1} more waiting after this one.`}</AppText>
              ) : null}
              {problem !== null ? <AppText variant="caption" tone="danger">{problemCopy(problem)}</AppText> : null}
              <View style={{ gap: spacing.sm }}>
                <Button
                  label={busy === "approve" ? "Approving…" : "Approve"}
                  disabled={busy !== null}
                  onPress={() => void approve()}
                />
                <Button
                  label={busy === "deny" ? "Removing…" : "Not me"}
                  variant="destructive"
                  disabled={busy !== null}
                  onPress={() => void deny()}
                />
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Decide later"
                  disabled={busy !== null}
                  onPress={() => e2ee.snoozePending(first.deviceId)}
                  style={{ alignItems: "center", paddingVertical: spacing.sm }}
                >
                  <AppText variant="bodyBold" tone="secondary">Decide later</AppText>
                </Pressable>
              </View>
            </>
          ) : null}
        </View>
      </View>
    </Modal>
  );
}
