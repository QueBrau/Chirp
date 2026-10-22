/**
 * Reset security (board c463, E2EE milestone M5, design section 8): for a phone that is waiting
 * for approval and cannot get it, because the other phone is lost, or this is a reinstall and
 * the server still lists the old install. It removes every OTHER phone from the account and
 * makes this one the account's first phone.
 *
 * This screen IS the confirmation, so it says plainly what happens before the one button that
 * does it: the other phones are removed, contacts will see that the security code changed and
 * must accept it before they can send, and older messages stay on the old phone. The work is
 * `engine.resetSecurity()`, which is bounded (it cannot loop) and never removes the phone being
 * used; this screen only words what it did. Copy makes no claim about encryption.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useState } from "react";
import { View } from "react-native";

import { AppText, Button, Card, EmptyState, Screen } from "@/components";
import { useE2ee } from "@/crypto/E2eeProvider";
import { e2ee } from "@/crypto/e2eeRuntime";
import { E2eeTrustError, type TrustFailureCode } from "@/crypto/e2eeTypes";
import { spacing, typography, useTheme } from "@/theme";

type Phase = "confirm" | "working" | "done" | "failed";

function failureCopy(code: TrustFailureCode | null, phoneIsSetUp: boolean): string {
  // After the other phones are removed a failure can leave this phone unfinished; say which.
  const aftermath = phoneIsSetUp
    ? "Nothing on this phone was changed."
    : "Your other phones were removed, but this phone isn't set up yet. Try again to finish.";
  switch (code) {
    case "network":
      return `Couldn't reach the server. ${aftermath}`;
    case "reset_incomplete":
      return "Another phone was approved while this ran, so this phone is not your first phone. Check your other phone, or try again.";
    case "account_deletion":
      return "Security can't be reset while this account is being deleted.";
    case "not_pending":
      return "Reset security is only for a phone that is waiting for approval.";
    default:
      return `Couldn't reset security. ${aftermath}`;
  }
}

function Consequence({ icon, text }: { icon: "x-circle" | "user-check" | "users" | "clock"; text: string }) {
  const palette = useTheme();
  return (
    <View style={{ flexDirection: "row", alignItems: "flex-start", gap: spacing.md }}>
      <Feather name={icon} size={typography.headline.fontSize} color={palette.inkSecondary} />
      <AppText variant="body" style={{ flex: 1 }}>{text}</AppText>
    </View>
  );
}

export default function ResetSecurityScreen() {
  const router = useRouter();
  const state = useE2ee();
  const [phase, setPhase] = useState<Phase>("confirm");
  const [failure, setFailure] = useState<TrustFailureCode | null>(null);
  const [phoneIsSetUp, setPhoneIsSetUp] = useState(true);

  const run = async () => {
    setPhase("working");
    setFailure(null);
    try {
      if (e2ee.getState().status === "error") {
        // A reset that failed after the other phones were removed leaves an error state; the
        // ordinary start finishes the job (this phone registers as the first phone).
        const next = await e2ee.recheck("manual");
        if (next.status !== "ready") throw new E2eeTrustError("network");
      } else {
        await e2ee.resetSecurity();
      }
      setPhase("done");
    } catch (error) {
      setFailure(error instanceof E2eeTrustError ? error.code : "rejected");
      setPhoneIsSetUp(e2ee.getState().status !== "error");
      setPhase("failed");
    }
  };

  const back = () => router.replace("/messages");

  if (phase === "done") {
    return (
      <Screen title="Security was reset" subtitle="This phone is ready" showBack={false}>
        <View style={{ gap: spacing.lg }}>
          <Card>
            <AppText variant="body">
              This phone is now your account's first phone. People you message will see that your security code changed,
              and they have to accept it before they can send to you.
            </AppText>
          </Card>
          <Button label="Back to messages" onPress={back} />
        </View>
      </Screen>
    );
  }

  const waiting = state.status === "pending_approval" || state.status === "error" || phase !== "confirm";
  return (
    <Screen title="Reset security" subtitle="For a phone you can't approve">
      {!waiting ? (
        <EmptyState
          title="Nothing to reset"
          message={
            state.status === "ready"
              ? "This phone is already approved, so there is nothing to reset."
              : "Reset security is only for a phone that is waiting for approval."
          }
          actionLabel="Back to messages"
          onAction={back}
        />
      ) : (
        <View style={{ gap: spacing.lg }}>
          <AppText variant="body" tone="secondary">
            Use this if you can't approve this phone from your other phone, for example because you lost it, or you
            reinstalled Chirp and the old copy is gone.
          </AppText>
          <Card>
            <View style={{ gap: spacing.lg }}>
              <AppText variant="headline">What happens</AppText>
              <Consequence icon="x-circle" text="Every other phone on your account is removed." />
              <Consequence icon="user-check" text="This phone becomes your account's first phone." />
              <Consequence
                icon="users"
                text="People you message will see that your security code changed. They have to accept it before they can send to you."
              />
              <Consequence icon="clock" text="Older messages stay on the old phone. This phone starts with no history." />
            </View>
          </Card>
          {phase === "failed" ? (
            <AppText variant="caption" tone="danger">{failureCopy(failure, phoneIsSetUp)}</AppText>
          ) : null}
          <Button
            label={phase === "working" ? "Resetting…" : phase === "failed" ? "Try again" : "Reset security"}
            variant="destructive"
            disabled={phase === "working"}
            onPress={() => void run()}
          />
          <Button label="Cancel" variant="ghost" disabled={phase === "working"} onPress={() => router.back()} />
        </View>
      )}
    </Screen>
  );
}
