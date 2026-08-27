/**
 * Verify your .edu — the designed destination for c88's 403 (board c90).
 *
 * Jose's call, Aug 16: an unverified user opening Chirps or the campus feed sees a REAL
 * SCREEN explaining they need to confirm an .edu for their school, WITH THE FORM ON IT,
 * rather than an empty feed (indistinguishable from a broken app) or a bare error toast.
 * c110 gave the two tabs an honest refusal; this is the place that refusal sends them.
 *
 * EVERY STATE A CODE FLOW ACTUALLY HAS IS A SCREEN STATE HERE, not a toast: enter your
 * address, code sent, code wrong, code expired, too many guesses, asked too often,
 * verified. Each one tells the user something different about what to do next, and
 * collapsing any pair of them produces the wrong instruction.
 *
 * TWO ENTRY COPIES, not one. A user who has never verified is being asked to prove
 * something; a user whose yearly re-check lapsed is being asked to prove it AGAIN.
 * Telling a returning student "confirm your school" as though they have never been here
 * is how you lose them, which is why the status endpoint returns verified_at even when
 * it refuses.
 *
 * THE ONE THAT WILL ACTUALLY HAPPEN TODAY is email_send_failed. c73's domain purchase is
 * on the backlog by Jose's decision, and Resend will not deliver to anyone but our own
 * account address without a verified sending domain. So a real student pressing "Send my
 * code" gets a 502 — and the honest thing is to say the code cannot be sent yet, not
 * "something went wrong", which would send them round the loop retyping a correct
 * address. Verified against the live provider, not guessed.
 */
import { useRouter } from "expo-router";
import { useState } from "react";
import { TextInput, View } from "react-native";

import {
  redeemCampusVerification,
  startCampusVerification,
  type CampusVerificationStatus,
} from "@/api/auth";
import { ApiError } from "@/api/client";
import { useCampusAccess, useSession } from "@/auth";
import { AppText, Button, Card, Screen } from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

type Phase = "email" | "code" | "done";

export default function VerifyCampusScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { refresh } = useSession();
  const access = useCampusAccess();

  const [phase, setPhase] = useState<Phase>("email");
  const [eduEmail, setEduEmail] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);

  // A lapsed verification is a different conversation from a first one.
  const lapsed = access === "lapsed";

  const send = async () => {
    setBusy(true);
    setError(null);
    try {
      await startCampusVerification(eduEmail.trim());
      setSentTo(eduEmail.trim().toLowerCase());
      setCode("");
      setPhase("code");
    } catch (err) {
      setError(sendError(err));
    } finally {
      setBusy(false);
    }
  };

  const redeem = async () => {
    setBusy(true);
    setError(null);
    try {
      const status: CampusVerificationStatus = await redeemCampusVerification(code.trim());
      if (!status.verified) {
        // Belt and braces: a 200 that is somehow not verified must not read as success.
        setError("That didn't complete. Try requesting a new code.");
        return;
      }
      // Pull the new campus_verified_at into the session before leaving, or the tab
      // we return to re-renders against the stale "unverified" answer and refuses again.
      await refresh();
      setPhase("done");
    } catch (err) {
      setError(redeemError(err));
    } finally {
      setBusy(false);
    }
  };

  if (phase === "done") {
    return (
      <Screen
        title="You're verified"
        subtitle="Your campus feed and Chirps are open. Welcome in."
      >
        <View style={{ gap: spacing.xl }}>
          <Card>
            <AppText variant="body">
              We confirmed {sentTo ?? "your school address"}. You won't need to do this
              again for a year.
            </AppText>
          </Card>
          <Button label="Go to my campus" onPress={() => router.replace("/feed")} />
        </View>
      </Screen>
    );
  }

  if (phase === "code") {
    return (
      <Screen
        title="Check your email"
        subtitle={`We sent a 6-digit code to ${sentTo ?? "your school address"}. It expires in 15 minutes.`}
      >
        <View style={{ gap: spacing.xl }}>
          <Card>
            <View style={{ gap: spacing.md }}>
              <AppText variant="headline">Your code</AppText>
              <TextInput
                value={code}
                onChangeText={setCode}
                placeholder="000000"
                placeholderTextColor={palette.inkFaint}
                keyboardType="number-pad"
                autoCorrect={false}
                maxLength={6}
                style={{
                  ...typography.body,
                  color: palette.ink,
                  backgroundColor: palette.surfaceAlt,
                  borderRadius: radii.input,
                  paddingHorizontal: spacing.lg,
                  paddingVertical: spacing.md,
                }}
              />
              {error !== null ? (
                <AppText variant="caption" tone="danger">
                  {error}
                </AppText>
              ) : null}
              <Button
                label={busy ? "Checking..." : "Confirm"}
                disabled={code.trim().length === 0 || busy}
                onPress={() => void redeem()}
              />
            </View>
          </Card>

          <Button
            label="Use a different address"
            variant="ghost"
            disabled={busy}
            onPress={() => {
              setPhase("email");
              setError(null);
            }}
          />
        </View>
      </Screen>
    );
  }

  return (
    <Screen
      title={lapsed ? "Confirm your school again" : "Confirm your school"}
      subtitle={
        lapsed
          ? "It's been a year since you last confirmed your .edu address. One code and you're back in."
          : "Your campus feed and Chirps are for students at your school. Confirm your .edu address to unlock them."
      }
    >
      <View style={{ gap: spacing.xl }}>
        <Card>
          <View style={{ gap: spacing.md }}>
            <AppText variant="headline">School email</AppText>
            <TextInput
              value={eduEmail}
              onChangeText={setEduEmail}
              placeholder="you@yourschool.edu"
              placeholderTextColor={palette.inkFaint}
              keyboardType="email-address"
              autoCapitalize="none"
              autoCorrect={false}
              style={{
                ...typography.body,
                color: palette.ink,
                backgroundColor: palette.surfaceAlt,
                borderRadius: radii.input,
                paddingHorizontal: spacing.lg,
                paddingVertical: spacing.md,
              }}
            />
            <AppText variant="caption" tone="tertiary">
              We only use this to check you go here. It stays off your profile.
            </AppText>
            {error !== null ? (
              <AppText variant="caption" tone="danger">
                {error}
              </AppText>
            ) : null}
            <Button
              label={busy ? "Sending..." : "Send my code"}
              disabled={eduEmail.trim().length === 0 || busy}
              onPress={() => void send()}
            />
          </View>
        </Card>

        <Button label="Not now" variant="ghost" onPress={() => router.back()} />
      </View>
    </Screen>
  );
}

/** Copy for a failed send. Each branch tells the user something different to do. */
function sendError(err: unknown): string {
  if (!(err instanceof ApiError)) return "Something went wrong. Try again.";
  if (err.status === 400 && err.detail === "unrecognized_edu_domain") {
    // The address is fine; we simply do not know that school yet. Blaming the user's
    // typing here would send them round the loop retyping something correct.
    return "We don't recognize that school's email domain yet. Check the address, or let us know which school you're at.";
  }
  if (err.status === 400) {
    return "That doesn't look like an email address. Check it and try again.";
  }
  if (err.status === 429) {
    return "That's a few codes in a short time. Give it fifteen minutes and try again.";
  }
  if (err.status === 502 || err.status === 503) {
    // 502 email_send_failed — the provider rejected or failed the send. c73 is on the
    // backlog, so this is the LIKELY path for a real student today.
    // 503 email_not_configured — the deployment set email_provider=resend with no key
    // (email_service._resend_api_key). Needs a misconfigured deploy to reach, since
    // email_provider defaults to "log" and prod has a key (c135), but it used to fall
    // through to the generic retry below — which tells a student to try again at the
    // one moment retrying cannot possibly work, and sends them round the loop
    // retyping an address that was correct the first time. That is the exact failure
    // this mapper exists to prevent (board c158).
    //
    // ONE branch, not two, because the two differ only operationally. The user's
    // situation is identical either way: codes cannot be sent, it is our fault, and
    // there is nothing for them to fix. Two branches would mean two copies of the same
    // sentence to keep in sync.
    return "We can't send verification codes just yet — that's on us, not you. Hang tight; your org tools all still work in the meantime.";
  }
  return "Something went wrong. Try again.";
}

/** Copy for a failed redemption. Wrong, expired and exhausted are three different fixes. */
function redeemError(err: unknown): string {
  if (!(err instanceof ApiError)) return "Something went wrong. Try again.";
  if (err.status === 403 && err.detail === "verification_expired") {
    return "That code has expired. Request a new one and it'll arrive in a moment.";
  }
  if (err.status === 403 && err.detail === "verification_code_invalid") {
    return "That code isn't right. Check the email and try again.";
  }
  if (err.status === 429) {
    return "Too many tries on that code. Request a fresh one to start over.";
  }
  if (err.status === 404) {
    return "That code is no longer active. Request a new one.";
  }
  return "Something went wrong. Try again.";
}
