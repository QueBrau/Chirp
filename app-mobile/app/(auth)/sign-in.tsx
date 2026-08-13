/**
 * Sign-in per DESIGN.md §7: brand block (HeroCard's layered-View gradient) with
 * the "Chirp" wordmark, full-width pill Apple/Google/email buttons, caption
 * legal line.
 *
 * Real auth (milestone 1): "Continue with Email" reveals an email/password form
 * wired to src/auth/session.ts (Firebase Auth). Apple/Google stay visual-only —
 * expo-auth-session / expo-apple-authentication need native config that only
 * works in a dev build, so they still fall through to the mock flow for now
 * (see the caption under those buttons and /SETUP-FIREBASE.md).
 *
 * No Firebase project exists yet, so hasFirebaseConfig() is false today: the
 * email form's submit falls back to the same mock flow, with a small caption
 * making that explicit. The screen's resting state (before any tap) is
 * unchanged from the pre-auth mock version.
 */

import { useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import { hasFirebaseConfig, signInWithEmail, signUpWithEmail } from "@/auth";
import { AppText, Button, HeroCard, Screen } from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

type EmailAuthMode = "signin" | "signup";

export default function SignInScreen() {
  const router = useRouter();
  const palette = useTheme();

  const [showEmailForm, setShowEmailForm] = useState(false);
  const [authMode, setAuthMode] = useState<EmailAuthMode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const continueToOnboarding = () => router.push("/account-type");

  const resetEmailForm = () => {
    setShowEmailForm(false);
    setError(null);
    setSubmitting(false);
  };

  const submitEmailForm = async () => {
    setSubmitting(true);
    setError(null);
    try {
      if (hasFirebaseConfig()) {
        if (authMode === "signin") {
          await signInWithEmail(email.trim(), password);
        } else {
          await signUpWithEmail(email.trim(), password);
        }
      }
      // Demo mode (no Firebase project yet) falls straight into the mock flow.
      continueToOnboarding();
    } catch {
      setError("Couldn't sign you in. Check your email and password and try again.");
    } finally {
      setSubmitting(false);
    }
  };

  const canSubmit = email.trim().length > 0 && password.length > 0 && !submitting;

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

        {showEmailForm ? (
          <View style={{ gap: spacing.md }}>
            <TextInput
              value={email}
              onChangeText={setEmail}
              placeholder="Email"
              placeholderTextColor={palette.inkFaint}
              keyboardType="email-address"
              autoCapitalize="none"
              autoCorrect={false}
              textContentType="emailAddress"
              style={{
                ...typography.body,
                color: palette.ink,
                backgroundColor: palette.surfaceAlt,
                borderRadius: radii.input,
                paddingHorizontal: spacing.lg,
                paddingVertical: spacing.md,
              }}
            />
            <TextInput
              value={password}
              onChangeText={setPassword}
              placeholder="Password"
              placeholderTextColor={palette.inkFaint}
              secureTextEntry
              autoCapitalize="none"
              autoCorrect={false}
              textContentType={authMode === "signin" ? "password" : "newPassword"}
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

            {!hasFirebaseConfig() ? (
              <AppText variant="caption" tone="tertiary">
                Demo mode — Firebase not configured
              </AppText>
            ) : null}

            <Button
              label={
                submitting
                  ? "Please wait..."
                  : authMode === "signin"
                    ? "Sign in"
                    : "Create account"
              }
              disabled={!canSubmit}
              onPress={() => void submitEmailForm()}
            />

            <Pressable
              accessibilityRole="button"
              onPress={() => setAuthMode(authMode === "signin" ? "signup" : "signin")}
              style={{ alignItems: "center", paddingVertical: spacing.xs }}
            >
              <AppText variant="caption" tone="accent">
                {authMode === "signin"
                  ? "Need an account? Sign up"
                  : "Already have an account? Sign in"}
              </AppText>
            </Pressable>

            <Button label="Back" variant="ghost" onPress={resetEmailForm} />
          </View>
        ) : (
          <View style={{ gap: spacing.md }}>
            <Button label="Continue with Apple" variant="secondary" onPress={continueToOnboarding} />
            <Button label="Continue with Google" variant="secondary" onPress={continueToOnboarding} />
            <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
              Apple and Google sign-in arrive with the dev build.
            </AppText>
            <Button label="Continue with Email" onPress={() => setShowEmailForm(true)} />
          </View>
        )}

        <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
          By continuing, you agree to Chirp's Terms of Service and acknowledge our Privacy Policy.
        </AppText>
      </View>
    </Screen>
  );
}
