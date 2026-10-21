/**
 * Safety number (board c463, E2EE milestone M5, design section 8): the one 60-digit number
 * both people's phones compute from the same approved phones, shown as twelve groups of five
 * so two people can read it to each other and compare. Opened from the thread header and from
 * the hard-stop banner.
 *
 * The number is read from directories fetched right now, never a cache, because a person
 * comparing numbers must be looking at the phones as they are this second. If the contact's
 * code changed (a hard stop), the screen shows the NEW digits, says so, and offers "Accept
 * new code" bound to exactly what is on screen: if the directory moves again before the person
 * taps, the accept refuses and the screen reloads. "Mark as verified" is stored on this phone
 * only, against the number in force, so it clears itself the moment either side's phones change.
 *
 * Copy here makes no claim about encryption (verify:e2ee-claims): it says what to do with the
 * number, which is true whatever protects the messages.
 */

import { Feather } from "@expo/vector-icons";
import { useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useState } from "react";
import { Switch, View } from "react-native";

import { AppText, Button, Card, EmptyState, Screen } from "@/components";
import { useE2ee } from "@/crypto/E2eeProvider";
import { e2ee } from "@/crypto/e2eeRuntime";
import { groupSafetyNumber } from "@/crypto/e2eeTrust";
import { E2eeTrustError, type SafetyView, type TrustFailureCode } from "@/crypto/e2eeTypes";
import { confirmAction } from "@/lib/alert";
import { radii, spacing, typography, useTheme } from "@/theme";

const ROW_SIZE = 4;

/** What a failed load or action says, in plain words. */
function failureCopy(code: TrustFailureCode | null): string {
  switch (code) {
    case "network":
      return "Couldn't reach the server. Check your connection and try again.";
    case "peer_no_devices":
      return "They don't have a phone set up for messages yet, so there is no number to compare.";
    case "changed_again":
      return "Something changed while you were looking. The number below is the current one. Look again before deciding.";
    case "account_deletion":
      return "That can't be changed while an account in this conversation is being deleted.";
    case "not_ready":
      return "This phone isn't ready for messages yet.";
    case "blocked":
      return "Accept the new code first. Then you can mark what you accepted as verified.";
    default:
      return "Couldn't do that. Try again.";
  }
}

function failureOf(error: unknown): TrustFailureCode {
  return error instanceof E2eeTrustError ? error.code : "rejected";
}

function who(name: string | undefined): string {
  return name !== undefined && name.trim().length > 0 ? name.trim() : "them";
}

export default function SafetyNumberScreen() {
  const palette = useTheme();
  const { peer, name } = useLocalSearchParams<{ peer?: string; conversation?: string; name?: string }>();
  const { status } = useE2ee();
  const [view, setView] = useState<SafetyView | null>(null);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState<TrustFailureCode | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<TrustFailureCode | null>(null);

  const load = useCallback(async () => {
    if (typeof peer !== "string" || status !== "ready") {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setView(await e2ee.safetyView(peer));
      setFailure(null);
    } catch (error) {
      setFailure(failureOf(error));
    } finally {
      setLoading(false);
    }
  }, [peer, status]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const toggleVerified = async (next: boolean) => {
    if (view === null || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      await e2ee.setVerified(view.peerUserId, next, view.fingerprint);
    } catch (error) {
      setNotice(failureOf(error));
    } finally {
      setBusy(false);
      await load();
    }
  };

  const acceptNewCode = () => {
    if (view === null || busy) return;
    const ownProblem = view.state === "trusted" && view.ownState !== "trusted";
    confirmAction({
      title: ownProblem ? "Accept the phones on your account?" : "Accept their new security code?",
      message: ownProblem
        ? "Only accept if you recognize every phone signed in to your account."
        : `Only accept if the numbers match when you compare them with ${who(name)}, or you know why it changed. Messages you send afterwards will be readable on every phone they have.`,
      confirmLabel: ownProblem ? "Accept change" : "Accept new code",
      onConfirm: () => {
        setBusy(true);
        setNotice(null);
        e2ee
          .acceptIdentityChange(
            ownProblem ? (e2ee.getState().userId ?? "") : view.peerUserId,
            ownProblem ? view.ownFingerprint : view.peerFingerprint,
          )
          .catch((error: unknown) => setNotice(failureOf(error)))
          .finally(() => {
            setBusy(false);
            void load();
          });
      },
    });
  };

  const groups = view === null ? [] : groupSafetyNumber(view.number);
  const rows: string[][] = [];
  for (let i = 0; i < groups.length; i += ROW_SIZE) rows.push(groups.slice(i, i + ROW_SIZE));
  const stopped = view !== null && (view.state !== "trusted" || view.ownState !== "trusted");
  const ownProblem = view !== null && view.state === "trusted" && view.ownState !== "trusted";

  return (
    <Screen title="Safety number" subtitle={`Your messages with ${who(name)}`} onRefresh={load}>
      {status !== "ready" ? (
        <EmptyState
          title="Not available yet"
          message="Safety numbers appear once this phone is set up for messages."
        />
      ) : failure !== null && view === null ? (
        <EmptyState
          title="Couldn't load the safety number"
          message={failureCopy(failure)}
          actionLabel="Try again"
          onAction={() => void load()}
        />
      ) : view === null ? (
        <AppText variant="caption" tone="secondary">{loading ? "Loading the safety number…" : " "}</AppText>
      ) : (
        <View style={{ gap: spacing.lg }}>
          {stopped ? (
            <View
              accessibilityRole="alert"
              style={{ backgroundColor: palette.warningSoft, borderRadius: radii.input, padding: spacing.md, gap: spacing.sm }}
            >
              <View style={{ flexDirection: "row", alignItems: "flex-start", gap: spacing.sm }}>
                <Feather name="alert-circle" size={typography.body.fontSize} color={palette.ink} />
                <View style={{ flex: 1, gap: spacing.xs }}>
                  <AppText variant="bodyBold">
                    {ownProblem
                      ? "The phones on your account changed in a way this phone can't confirm."
                      : view.state === "identity_changed"
                        ? "Their security code changed."
                        : "They added a device that wasn't approved."}
                  </AppText>
                  <AppText variant="caption" tone="secondary">
                    {ownProblem
                      ? "Sending is paused. These digits are for your current phones."
                      : "Sending is paused. These are the new digits. Compare them with them in person before you accept."}
                  </AppText>
                </View>
              </View>
              <Button
                label={busy ? "Working…" : ownProblem ? "Accept change" : "Accept new code"}
                variant="neutral"
                disabled={busy}
                onPress={acceptNewCode}
              />
            </View>
          ) : null}

          <Card>
            <View style={{ gap: spacing.lg }}>
              <AppText variant="body">
                {`Compare these numbers with ${who(name)} in person. If they match, only your phones and theirs can read your messages.`}
              </AppText>
              <View
                accessible
                accessibilityLabel={`Safety number: ${groups.join(", ")}`}
                style={{ backgroundColor: palette.surfaceAlt, borderRadius: radii.input, padding: spacing.lg, gap: spacing.md }}
              >
                {rows.map((row, index) => (
                  <View key={index} style={{ flexDirection: "row", justifyContent: "space-between" }}>
                    {row.map((group, column) => (
                      <AppText key={column} variant="stat" style={{ letterSpacing: 1 }}>
                        {group}
                      </AppText>
                    ))}
                  </View>
                ))}
              </View>
            </View>
          </Card>

          <Card>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
              <View style={{ flex: 1, gap: spacing.xs }}>
                <AppText variant="headline">Mark as verified</AppText>
                <AppText variant="caption" tone="secondary">
                  {stopped
                    ? "Accept the new code first. Then you can mark what you accepted as verified."
                    : "Saved on this phone only. It clears by itself if either of you adds, replaces or removes a phone."}
                </AppText>
              </View>
              <Switch
                accessibilityLabel="Mark as verified"
                value={view.verified}
                disabled={busy || stopped}
                onValueChange={(next) => void toggleVerified(next)}
                trackColor={{ true: palette.accent, false: palette.surfaceAlt }}
              />
            </View>
          </Card>

          {notice !== null ? <AppText variant="caption" tone="danger">{failureCopy(notice)}</AppText> : null}
          {failure !== null ? <AppText variant="caption" tone="danger">{failureCopy(failure)}</AppText> : null}
        </View>
      )}
    </Screen>
  );
}
