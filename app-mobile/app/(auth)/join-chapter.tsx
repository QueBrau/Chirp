/** Join chapter: invite-code entry + explainer; landing for the chirp://join-chapter?code=... deep link. */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { TextInput, View } from "react-native";

import { joinChapter } from "@/api/chapters";
import { AppText, Button, Card, Screen } from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

export default function JoinChapterScreen() {
  const router = useRouter();
  const palette = useTheme();
  // Deep link `chirp://join-chapter?code=...` (and https app links) prefill the code.
  const { code: linkCode } = useLocalSearchParams<{ code?: string }>();
  const [code, setCode] = useState(linkCode ?? "");
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const join = async () => {
    setJoining(true);
    setError(null);
    try {
      await joinChapter(code.trim());
      router.replace("/feed");
    } catch {
      setError("That invite code didn't work. Check it and try again.");
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
