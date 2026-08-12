/** Sign-in: Apple / Google / email entry points — visual only until Firebase Auth lands. */

import { useRouter } from "expo-router";
import { View } from "react-native";

import { AppText, Button, Screen } from "@/components";
import { spacing } from "@/theme";

export default function SignInScreen() {
  const router = useRouter();

  // TODO(milestone-1): wire Firebase Auth (Apple Sign-In required alongside Google, SPEC §7).
  const continueToOnboarding = () => router.push("/account-type");

  return (
    <Screen scroll={false}>
      <View style={{ flex: 1, justifyContent: "center", gap: spacing.md }}>
        <View style={{ alignItems: "center", marginBottom: spacing.xxl, gap: spacing.sm }}>
          <AppText variant="display" tone="accent">
            Chirp
          </AppText>
          <AppText variant="body" tone="secondary" style={{ textAlign: "center" }}>
            Your chapter, in one place.
          </AppText>
        </View>

        <Button label="Continue with Apple" variant="secondary" onPress={continueToOnboarding} />
        <Button label="Continue with Google" variant="secondary" onPress={continueToOnboarding} />
        <Button label="Continue with Email" onPress={continueToOnboarding} />

        <AppText variant="caption" tone="tertiary" style={{ textAlign: "center", marginTop: spacing.lg }}>
          Sign-in is a placeholder — Firebase Auth arrives in milestone 1.
        </AppText>
      </View>
    </Screen>
  );
}
