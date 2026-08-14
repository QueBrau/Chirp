/** Join chapter: invite-code entry + explainer; landing for the chirp://join-chapter?code=... deep link. */

import { Redirect, useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { TextInput, View } from "react-native";

import { joinChapter } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { useSession } from "@/auth";
import { AppText, Button, Card, Screen } from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

export default function JoinChapterScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { status } = useSession();
  // Deep link `chirp://join-chapter?code=...` (and https app links) prefill the code.
  const { code: linkCode } = useLocalSearchParams<{ code?: string }>();
  const [code, setCode] = useState(linkCode ?? "");
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Unguarded deep link fix: a signed-out visitor who opens the invite link goes
  // to sign-in first, with the code carried through so it lands them right back
  // here (or on /account-type -> here) once they're in. "unregistered" is left
  // alone — the normal account-type flow already routes here after bootstrap.
  if (status === "signedOut") {
    return <Redirect href={linkCode ? `/sign-in?code=${encodeURIComponent(linkCode)}` : "/sign-in"} />;
  }
  if (status === "unregistered") {
    // Signed in but never bootstrapped (e.g. app killed mid-onboarding): finish
    // account-type first; the code rides along and comes back here after.
    return (
      <Redirect
        href={linkCode ? `/account-type?code=${encodeURIComponent(linkCode)}` : "/account-type"}
      />
    );
  }

  const join = async () => {
    setJoining(true);
    setError(null);
    try {
      await joinChapter(code.trim());
      router.replace("/feed");
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 400)) {
        setError("That invite code didn't work. Check it and try again.");
      } else if (err instanceof ApiError && err.status === 409) {
        setError("You are already in this chapter.");
        router.replace("/feed");
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
              Codes look like this — ask your chapter's e-board if you don't have one yet.
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
