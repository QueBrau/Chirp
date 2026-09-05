/** Join chapter: invite-code entry + explainer; landing for the chirp://join-chapter?code=... deep link. */

import { Redirect, useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { TextInput, View } from "react-native";

import { joinChapter } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { useSession, withInviteCode } from "@/auth";
import { AppText, Button, Card, Screen } from "@/components";
import { inputField, spacing, useTheme } from "@/theme";

export default function JoinChapterScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { status, refresh } = useSession();
  // Deep link `chirp://join-chapter?code=...` (and https app links) prefill the code.
  const { code: linkCode } = useLocalSearchParams<{ code?: string }>();
  const [code, setCode] = useState(linkCode ?? "");
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Hold the form back until the session resolves — rendering during "loading"
  // would let a fast tap fire a tokenless join on cold start.
  if (status === "loading") return null;
  // Unguarded deep link fix: a signed-out visitor who opens the invite link goes
  // to sign-in first, with the code carried through so it lands them right back
  // here (or on /account-type -> here) once they're in.
  if (status === "signedOut") {
    return <Redirect href={withInviteCode("/sign-in", linkCode)} />;
  }
  if (status === "unregistered") {
    // Signed in but never bootstrapped (e.g. app killed mid-onboarding): finish
    // account-type first; the code rides along and comes back here after.
    return <Redirect href={withInviteCode("/account-type", linkCode)} />;
  }

  const join = async () => {
    setJoining(true);
    setError(null);
    try {
      await joinChapter(code.trim());
      // c332: AWAIT the refresh, then route REGARDLESS of what it returns.
      //
      // The await exists only to close the stale-render window on the success path —
      // `void refresh()` let /feed mount against a session that did not know about the
      // membership yet.
      //
      // ROUTING REGARDLESS IS THE LOAD-BEARING HALF. By the time joinChapter resolves
      // the join is complete server-side: the membership row exists and the invite's
      // seat is spent. A failed refresh says nothing about that, so surfacing it as an
      // error would be a false statement AND an instruction to re-enter a code that is
      // now dead. Residual staleness is what the destination's own failure states are
      // for, and this week made those comprehensive (c299/c313/c316/c317/c330).
      //
      // DELIBERATELY NOT account-type.tsx's shape, which awaits refresh and falls back
      // to an error when it does not settle (line ~155). That path gates a DIFFERENT
      // question — whether the account exists at all — where an unsettled session means
      // the app genuinely does not know, and proceeding would bounce off the guards.
      // Here the answer is already known. Do not harmonise these two; the asymmetry is
      // the point, same as the c312/c316 status sets that also look copyable and are not.
      await refresh();
      router.replace("/feed");
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 400)) {
        setError("That invite code didn't work. Check it and try again.");
      } else if (err instanceof ApiError && err.status === 409) {
        // Already a member — that's success from the user's point of view, and the
        // membership predates this attempt entirely, so the same rule applies even
        // more plainly: await to close the stale window, route either way.
        await refresh();
        router.replace("/feed");
      } else if (err instanceof ApiError && err.status === 403 && err.detail === "invite_expired") {
        setError("This invite code has expired. Ask your e-board for a fresh one.");
      } else if (err instanceof ApiError && err.status === 403 && err.detail === "invite_exhausted") {
        // c105: codes now have a redemption budget, so "used up" is a real,
        // reachable state and needs its own line. The generic message would tell
        // a student to re-check a code they typed correctly.
        setError("This invite code has been used up. Ask your e-board for a fresh one.");
      } else if (err instanceof ApiError && err.status === 403 && err.detail === "invite_revoked") {
        setError("This invite code was turned off by your e-board. Ask them for a new one.");
      } else {
        setError("Something went wrong. Try again.");
      }
    } finally {
      setJoining(false);
    }
  };

  return (
    <Screen
      title="Join your chapter"
      subtitle="Enter the invite code from your chapter's e-board, or open their invite link."
    >
      <View style={{ gap: spacing.xl }}>
        <Card>
          <View style={{ gap: spacing.md }}>
            <AppText variant="headline">Invite code</AppText>
            <TextInput
              value={code}
              onChangeText={setCode}
              placeholder="e.g. SIGCHI-EM-F26"
              placeholderTextColor={palette.inkFaint}
              autoCapitalize="characters"
              autoCorrect={false}
              style={inputField(palette)}
            />
            <AppText variant="caption" tone="tertiary">
              Codes look like this. Ask your chapter's e-board if you don't have one yet.
            </AppText>
            {error !== null ? (
              <AppText variant="caption" tone="danger">
                {error}
              </AppText>
            ) : null}
            <Button
              label={joining ? "Joining..." : "Join chapter"}
              disabled={code.trim().length === 0 || joining}
              onPress={() => void join()}
            />
          </View>
        </Card>

        <Button label="Skip for now" variant="ghost" onPress={() => router.replace("/feed")} />
      </View>
    </Screen>
  );
}
