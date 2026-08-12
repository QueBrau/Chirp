/**
 * Sign-in per DESIGN.md §7: brand block (HeroCard's layered-View gradient) with
 * the "Chirp" wordmark, full-width pill Apple/Google/email buttons, caption
 * legal line. Visual only until Firebase Auth lands.
 */

import { useRouter } from "expo-router";
import { View } from "react-native";

import { AppText, Button, HeroCard, Screen } from "@/components";
import { spacing } from "@/theme";

export default function SignInScreen() {
  const router = useRouter();

  // TODO(milestone-1): wire Firebase Auth (Apple Sign-In required alongside Google, SPEC §7).
  const continueToOnboarding = () => router.push("/account-type");

  return (
    <Screen scroll={false}>
      <View style={{ flex: 1, justifyContent: "center", gap: spacing.xl }}>
        <HeroCard>
          <View style={{ alignItems: "center", gap: spacing.xs, paddingVertical: spacing.lg }}>
            <AppText variant="display" tone="onAccent" style={{ letterSpacing: -0.5 }}>
              Chirp
            </AppText>
            <AppText variant="body" tone="onAccent" style={{ textAlign: "center" }}>
              Your campus, in one place.
            </AppText>
          </View>
        </HeroCard>

        <View style={{ gap: spacing.md }}>
          <Button label="Continue with Apple" variant="secondary" onPress={continueToOnboarding} />
          <Button label="Continue with Google" variant="secondary" onPress={continueToOnboarding} />
          <Button label="Continue with Email" onPress={continueToOnboarding} />
        </View>

        <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
          By continuing, you agree to Chirp's Terms of Service and acknowledge our Privacy Policy.
        </AppText>
      </View>
    </Screen>
  );
}
