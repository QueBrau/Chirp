/**
 * Sign-in per DESIGN.md §7: brand block (HeroCard's layered-View gradient) with
 * the "Chirp" wordmark, full-width pill Apple/Google/email buttons, caption
 * legal line.
 *
 * Real auth (milestone 1): "Continue with Email" reveals an email/password form
 * wired to src/auth/session.ts (Firebase Auth). Apple runs the real native
 * flow (src/auth/appleSignIn.ts, c314) when isAppleSignInAvailable() is true —
 * iOS with the OS-level capability present. Google runs the real native flow
 * (src/auth/googleSignIn.ts, c169) when isGoogleSignInAvailable() is true —
 * iOS with the OAuth clients configured. Each falls back independently to the
 * honest-stub behavior when unavailable. Neither path may fall through to the
 * mock onboarding flow: an unavailable OR failed provider is not an
 * authenticated session (c89).
 *
 * When Firebase is unavailable, the email form keeps its existing demo-mode
 * behavior. Social buttons use a separate honest error state so they cannot
 * accidentally grant access while native setup is unavailable or pending.
 */

import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import {
  getAuthErrorMessage,
  getPasswordLengthError,
  hasFirebaseConfig,
  isAppleSignInAvailable,
  isGoogleSignInAvailable,
  signInWithApple,
  signInWithEmail,
  signInWithGoogle,
  signOutUser,
  signUpWithEmail,
  socialAuthUnavailableMessage,
  type SocialAuthProvider,
  useSession,
  withInviteCode,
} from "@/auth";
import { AppText, Button, HeroCard, Screen } from "@/components";
import { inputField, spacing, useTheme } from "@/theme";

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
  const [socialError, setSocialError] = useState<string | null>(null);
  // Resolved asynchronously (expo-apple-authentication's own OS-level check),
  // so this starts false and the Apple button behaves like the honest stub
  // until it resolves — never a flash of "available" that isn't real yet.
  const [appleAvailable, setAppleAvailable] = useState(false);
  /**
   * Non-null between "Firebase accepted the credential" and "the session
   * resolved" — navigation is deferred across that window — and it holds the
   * mode the user actually SUBMITTED with, not the mode the toggle happens to
   * be showing when the session lands.
   *
   * Those came apart in review. The toggle underneath the submit button stayed
   * live during the wait, so a user who read "Please wait..." as stuck could tap
   * "Need an account? Sign up", flip authMode, and have the routing decision
   * read the flipped value once /auth/me returned — sending a fully registered
   * returning user to /account-type to answer "who are you?" again, which is
   * precisely the c45 regression the routing comment claims to prevent. With an
   * invite code in tow they lost /join-chapter too. The window is ~1s normally
   * but the budget is 15s, so a cold Cloud Run start makes it very reachable.
   *
   * The toggle is disabled while submitting as well, but this is the fix that
   * matters: the decision is now carried from the moment of submit instead of
   * re-read from mutable UI state, so it cannot depend on the toggle being
   * unreachable.
   */
  const [submittedMode, setSubmittedMode] = useState<EmailAuthMode | null>(null);

  // New identities keep the invite code in tow through account-type, which
  // forwards it to join-chapter after bootstrap.
  const continueToOnboarding = () => router.push(withInviteCode("/account-type", inviteCode));

  /**
   * Social auth is deliberately a no-op until native provider credentials are
   * configured.  In particular, do not call continueToOnboarding() here: that
   * would make a button that looks like authentication silently bypass it.
   */
  const handleUnavailableSocialProvider = (provider: SocialAuthProvider) => {
    setSocialError(socialAuthUnavailableMessage(provider));
  };

  // Synchronous (config + platform checks only, no OS query), so the button
  // can read it inline — unlike the Apple check below there is no async window
  // where the button might flash "available" before the answer arrives.
  const googleAvailable = isGoogleSignInAvailable();

  // Checked once on mount rather than inline in the button's onPress: the
  // check itself is async (an OS-level query), and the button must decide
  // synchronously whether a tap runs the real flow or falls back to the
  // honest stub.
  useEffect(() => {
    let cancelled = false;
    void isAppleSignInAvailable().then((available) => {
      if (!cancelled) setAppleAvailable(available);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Runs the real native Apple flow (src/auth/appleSignIn.ts). Only called
   * when appleAvailable is true — see the Apple Button's onPress below.
   *
   * Mirrors submitEmailForm's post-credential handling exactly: a successful
   * exchange is a Firebase credential, not yet a backend session, so this
   * sets submittedMode rather than navigating directly. The effect above
   * (written for email) owns the wait and the eventual routing — it keys off
   * submittedMode alone, not off which provider produced it. "signin" is the
   * correct mode for BOTH a returning Apple user and a brand-new one: a
   * returning user resolves to status "ready" and routeAfterAuth("signin")
   * sends them to the tabs/join-chapter exactly like a returning email user;
   * a brand-new Apple identity resolves to status "unregistered", and that
   * branch of the effect already goes to continueToOnboarding() unconditionally
   * regardless of mode. There is no "signup" concept for a social sign-in —
   * the user never chose to sign in vs. sign up, Apple just handed back a
   * credential — so reusing "signin" here is not a mismatch, it is the only
   * mode value where routeAfterAuth's own logic produces the right answer in
   * both cases.
   */
  const handleAppleSignIn = async () => {
    setSocialError(null);
    setSubmitting(true);
    const outcome = await signInWithApple();
    switch (outcome.status) {
      case "success":
        setSubmittedMode("signin");
        return;
      case "cancelled":
        // The user dismissed the system sheet. Not an error (c314): show
        // nothing, no error text, no alert.
        setSubmitting(false);
        return;
      case "error":
        setSubmitting(false);
        setSocialError(outcome.message);
        return;
    }
  };

  const handleApplePress = () => {
    if (!appleAvailable) {
      handleUnavailableSocialProvider("apple");
      return;
    }
    void handleAppleSignIn();
  };

  /**
   * Runs the real native Google flow (src/auth/googleSignIn.ts). Only called
   * when googleAvailable is true — see the Google Button's onPress below.
   * Same post-credential contract as handleAppleSignIn above — see that
   * comment for why "signin" is the only submittedMode value that routes both
   * a returning and a brand-new social identity correctly.
   */
  const handleGoogleSignIn = async () => {
    setSocialError(null);
    setSubmitting(true);
    const outcome = await signInWithGoogle();
    switch (outcome.status) {
      case "success":
        setSubmittedMode("signin");
        return;
      case "cancelled":
        // The user dismissed the Google sheet. Not an error (c89/c314's
        // rule): show nothing, no error text, no alert.
        setSubmitting(false);
        return;
      case "error":
        setSubmitting(false);
        setSocialError(outcome.message);
        return;
    }
  };

  const handleGooglePress = () => {
    if (!googleAvailable) {
      handleUnavailableSocialProvider("google");
      return;
    }
    void handleGoogleSignIn();
  };

  /**
   * Post sign-in/up routing. "Sign in" means a returning user: with an invite
   * code in tow they go straight to redeem it; otherwise they land on the tabs,
   * whose auth guard resolves the session and only bounces a genuinely
   * unregistered identity back to account-type (c45 — a returning registered
   * user no longer re-answers "who are you?" on every sign-in). "Sign up" is a
   * brand-new Firebase identity that still needs account-type regardless of any
   * code. Demo mode (no Firebase project) keeps the mock onboarding walk.
   */
  const routeAfterAuth = (mode: EmailAuthMode) => {
    if (mode === "signin" && inviteCode) {
      router.push(withInviteCode("/join-chapter", inviteCode));
      return;
    }
    if (mode === "signin" && hasFirebaseConfig()) {
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
    if (submittedMode === null) return;
    if (status === "ready") {
      setSubmittedMode(null);
      setSubmitting(false);
      routeAfterAuth(submittedMode);
    } else if (status === "unregistered") {
      // Signed in but bootstrap never finished (app killed mid-onboarding).
      // account-type is where the tabs guard would send them anyway.
      setSubmittedMode(null);
      setSubmitting(false);
      continueToOnboarding();
    }
    // routeAfterAuth/continueToOnboarding close over router and inviteCode only,
    // and the mode is passed in explicitly rather than read from state — see
    // submittedMode's declaration for why that distinction is load-bearing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submittedMode, status]);

  /**
   * The session never settling is a real outcome, not an impossible one: a
   * backend that 500s leaves SessionProvider retrying and the status pinned at
   * "signedOut" forever. Say so rather than spinning "Please wait..." until the
   * user force-quits. The credential is genuinely good at this point, so the
   * message must not blame their password.
   */
  useEffect(() => {
    if (submittedMode === null) return;
    const timer = setTimeout(() => {
      setSubmittedMode(null);
      setSubmitting(false);
      setError("You're signed in, but we couldn't load your account. Check your connection and try again.");
    }, SESSION_SETTLE_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [submittedMode]);

  /**
   * "Back" out of the email form. If the credential has already landed and we
   * are only waiting on the session, backing out must also SIGN OUT: otherwise
   * the user sits on the signed-out provider screen while genuinely
   * authenticated, the 15s timer is cancelled so no error ever appears, and the
   * Apple/Google buttons then walk a registered user through onboarding again.
   * Found in review alongside the toggle bug; same c45 family, lower frequency.
   */
  const resetEmailForm = () => {
    if (submittedMode !== null && hasFirebaseConfig()) {
      void signOutUser().catch(() => {
        // Nothing useful to tell the user — they asked to go back and they are
        // going back. SessionProvider's listener owns the state either way.
      });
    }
    setShowEmailForm(false);
    setError(null);
    setSocialError(null);
    setSubmitting(false);
    setSubmittedMode(null);
  };

  const submitEmailForm = async () => {
    // Captured now, deliberately: everything downstream routes off THIS value,
    // not off `authMode`, which the user can still change later.
    const mode = authMode;

    // Length is a sign-up-only check (getPasswordLengthError is a no-op on
    // sign-in - see its comment in src/auth/authErrors.ts) so the obvious case
    // answers instantly instead of waiting on a Firebase round trip, and never
    // tells a returning user their real password is "invalid".
    const lengthError = getPasswordLengthError(password, mode);
    if (lengthError !== null) {
      setError(lengthError);
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      if (hasFirebaseConfig()) {
        if (mode === "signin") {
          await signInWithEmail(email.trim(), password);
          // Stay put, still "submitting", until the session resolves. The
          // effect above owns the navigation from here.
          setSubmittedMode(mode);
          return;
        }
        await signUpWithEmail(email.trim(), password);
      }
      // Demo mode (no Firebase project yet) falls straight into the mock flow,
      // as does a brand-new sign-up: account-type is unguarded, and its
      // applyBootstrap() settles the session before anything guarded mounts.
      setSubmitting(false);
      routeAfterAuth(mode);
    } catch (err) {
      // c311: bind the error so its FirebaseError `.code` (auth/wrong-password,
      // auth/email-already-in-use, ...) reaches the mapper instead of being
      // discarded by an empty `catch {}` - see src/auth/authErrors.ts for what
      // each code means and why sign-in deliberately does NOT get more specific
      // than "email and password don't match" for user-not-found/wrong-password.
      setError(getAuthErrorMessage(err, mode));
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
              style={inputField(palette)}
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
              style={inputField(palette)}
            />

            {error !== null ? (
              <AppText variant="caption" tone="danger">
                {error}
              </AppText>
            ) : null}

            {!hasFirebaseConfig() ? (
              <AppText variant="caption" tone="tertiary">
                Demo mode. Firebase not configured
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
              disabled={submitting}
              onPress={() => setAuthMode(authMode === "signin" ? "signup" : "signin")}
              style={{ alignItems: "center", paddingVertical: spacing.xs, opacity: submitting ? 0.4 : 1 }}
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
            <Button
              label="Continue with Apple"
              variant="secondary"
              disabled={submitting}
              onPress={handleApplePress}
            />
            <Button
              label="Continue with Google"
              variant="secondary"
              disabled={submitting}
              onPress={handleGooglePress}
            />
            {appleAvailable && googleAvailable ? null : (
              <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
                {appleAvailable
                  ? "Google sign-in is not connected in this build yet. Use Apple or Email."
                  : googleAvailable
                    ? "Apple sign-in is not connected in this build yet. Use Google or Email."
                    : "Apple and Google sign-in are not connected in this build yet. Use Email instead."}
              </AppText>
            )}
            {socialError !== null ? (
              <AppText variant="caption" tone="danger" style={{ textAlign: "center" }}>
                {socialError}
              </AppText>
            ) : null}
            <Button
              label="Continue with Email"
              onPress={() => {
                setSocialError(null);
                setShowEmailForm(true);
              }}
            />
          </View>
        )}

        <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
          By continuing, you agree to Chirp's Terms of Service and acknowledge our Privacy Policy.
        </AppText>
      </View>
    </Screen>
  );
}
