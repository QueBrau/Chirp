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

import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import {
  hasFirebaseConfig,
  signInWithEmail,
  signUpWithEmail,
  useSession,
  withInviteCode,
} from "@/auth";
import { AppText, Button, HeroCard, Screen } from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

type EmailAuthMode = "signin" | "signup";

/**
 * How long to hold the screen waiting for the backend session after a good
 * credential (c94). SessionProvider retries a failed /auth/me three times at
 * 3s, so anything under ~12s would give up while it is still trying.
 */
const SESSION_SETTLE_TIMEOUT_MS = 15_000;

export default function SignInScreen() {
  const router = useRouter();
  const palette = useTheme();
  // c94: the guard on the far side of every post-sign-in route reads this.
  const { status } = useSession();
  // Carried through from an invite deep link that bounced an unauthenticated
  // visitor here via join-chapter's Redirect (chirp://join-chapter?code=...).
  const { code: inviteCode } = useLocalSearchParams<{ code?: string }>();

  const [showEmailForm, setShowEmailForm] = useState(false);
  const [authMode, setAuthMode] = useState<EmailAuthMode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // True between "Firebase accepted the credential" and "the session resolved".
  // Navigation is deferred across that window — see awaitSession below.
  const [awaitingSession, setAwaitingSession] = useState(false);

  // New identities keep the invite code in tow through account-type, which
  // forwards it to join-chapter after bootstrap.
  const continueToOnboarding = () => router.push(withInviteCode("/account-type", inviteCode));

  /**
   * Post sign-in/up routing. "Sign in" means a returning user: with an invite
   * code in tow they go straight to redeem it; otherwise they land on the tabs,
   * whose auth guard resolves the session and only bounces a genuinely
   * unregistered identity back to account-type (c45 — a returning registered
   * user no longer re-answers "who are you?" on every sign-in). "Sign up" is a
   * brand-new Firebase identity that still needs account-type regardless of any
   * code. Demo mode (no Firebase project) keeps the mock onboarding walk.
   */
  const continueAfterAuth = () => {
    if (authMode === "signin" && inviteCode) {
      router.push(withInviteCode("/join-chapter", inviteCode));
      return;
    }
    if (authMode === "signin" && hasFirebaseConfig()) {
      router.replace("/(tabs)/feed");
      return;
    }
    continueToOnboarding();
  };

  /**
   * c94 — navigate on the SESSION, never on the credential.
   *
   * `await signInWithEmail()` only proves Firebase accepted the password. The
   * backend session is a separate round trip: SessionProvider's auth listener
   * kicks off `loadMe()` and does not call setStatus until GET /auth/me comes
   * back, so for that whole window `status` is still "signedOut". Navigating
   * inside it mounts a destination that reads the stale value and sends us
   * straight back — (tabs)/_layout redirects to /sign-in, and join-chapter
   * redirects to /sign-in too, which for the invite path is an actual loop.
   * The user sees the provider buttons re-render and reads it as "nothing
   * happened", then a reload works, because a cold start waits in "loading".
   *
   * Sign-up never had this: account-type's applyBootstrap() flips the status
   * synchronously before it moves. So we do the same thing the honest way and
   * hold here until the session is a settled fact.
   */
  useEffect(() => {
    if (!awaitingSession) return;
    if (status === "ready") {
      setAwaitingSession(false);
      setSubmitting(false);
      continueAfterAuth();
    } else if (status === "unregistered") {
      // Signed in but bootstrap never finished (app killed mid-onboarding).
      // account-type is where the tabs guard would send them anyway.
      setAwaitingSession(false);
      setSubmitting(false);
      continueToOnboarding();
    }
    // continueAfterAuth/continueToOnboarding close over router + inviteCode +
    // authMode, none of which change while a submit is in flight.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [awaitingSession, status]);

  /**
   * The session never settling is a real outcome, not an impossible one: a
   * backend that 500s leaves SessionProvider retrying and the status pinned at
   * "signedOut" forever. Say so rather than spinning "Please wait..." until the
   * user force-quits. The credential is genuinely good at this point, so the
   * message must not blame their password.
   */
  useEffect(() => {
    if (!awaitingSession) return;
    const timer = setTimeout(() => {
      setAwaitingSession(false);
      setSubmitting(false);
      setError("You're signed in, but we couldn't load your account. Check your connection and try again.");
    }, SESSION_SETTLE_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [awaitingSession]);

  const resetEmailForm = () => {
    setShowEmailForm(false);
    setError(null);
    setSubmitting(false);
    setAwaitingSession(false);
  };

  const submitEmailForm = async () => {
    setSubmitting(true);
    setError(null);
    try {
      if (hasFirebaseConfig()) {
        if (authMode === "signin") {
          await signInWithEmail(email.trim(), password);
          // Stay put, still "submitting", until the session resolves. The
          // effect above owns the navigation from here.
          setAwaitingSession(true);
          return;
        }
        await signUpWithEmail(email.trim(), password);
      }
      // Demo mode (no Firebase project yet) falls straight into the mock flow,
      // as does a brand-new sign-up: account-type is unguarded, and its
      // applyBootstrap() settles the session before anything guarded mounts.
      setSubmitting(false);
      continueAfterAuth();
    } catch {
      setError("Couldn't sign you in. Check your email and password and try again.");
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
