/**
 * Sign-in / sign-up per DESIGN.md §7: ONE page (c385, braul, from a reference
 * shot) - oversized title, UnderlineField form, brand CTA, social row, footer
 * mode toggle, legal caption.
 *
 * WHAT c385 CHANGED IS THE MARKUP AND NOTHING ELSE. This screen carries a lot of
 * hard-won correctness that is invisible in a screenshot, and all of it survives
 * intact below: submittedMode captured AT SUBMIT rather than re-read from a live
 * toggle (the c45 regression), the 15s session-settle timeout, routeAfterAuth's
 * returning-vs-new split, signing out when backing out mid-wait, per-keystroke
 * error clearing (c320), demo mode, and the independent Apple/Google availability
 * checks with their honest-stub fallbacks. Read the comments before moving any of
 * it; each one names a bug that actually shipped.
 *
 * The two-stage shape is gone: there is no "Continue with Email" reveal and no
 * `showEmailForm`, because the reference puts the form, the CTA and the providers
 * on one screen. `resetEmailForm`'s sign-out-on-back survives as the mode toggle's
 * behaviour, which is now the only way to leave a pending wait.
 *
 * Real auth (milestone 1): the email/password form is wired to src/auth/session.ts
 * (Firebase Auth). Apple runs the real native
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
import { Pressable, View } from "react-native";

import {
  getAuthErrorMessage,
  getPasswordLengthError,
  hasFirebaseConfig,
  isAppleSignInAvailable,
  isGoogleSignInAvailable,
  isUserNotFoundError,
  sendPasswordReset,
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
import { AppText, Button, Screen, UnderlineField } from "@/components";
import { MIN_PASSWORD_LENGTH } from "@/lib/passwordPolicy";
import { canvasActionColor, spacing, useAppearance, useTheme } from "@/theme";

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
  const { campusColors } = useAppearance();
  // c94: the guard on the far side of every post-sign-in route reads this.
  const { status } = useSession();
  // Carried through from an invite deep link that bounced an unauthenticated
  // visitor here via join-chapter's Redirect (chirp://join-chapter?code=...).
  const { code: inviteCode } = useLocalSearchParams<{ code?: string }>();

  const [authMode, setAuthMode] = useState<EmailAuthMode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  /** Sign-up only. Cleared on every mode switch so a stale value can never be
   *  compared against a password typed later. */
  const [repeatPassword, setRepeatPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showRepeatPassword, setShowRepeatPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [socialError, setSocialError] = useState<string | null>(null);
  /** Neutral confirmation after a reset request. Deliberately not an error and
   *  deliberately the same text whether or not the address exists - see
   *  handleForgotPassword. */
  const [resetNotice, setResetNotice] = useState<string | null>(null);
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
   * Abandon a pending credential. If the credential has already landed and we are
   * only waiting on the session, leaving must also SIGN OUT: otherwise the user
   * sits on the signed-out screen while genuinely authenticated, the 15s timer is
   * cancelled so no error ever appears, and the Apple/Google buttons then walk a
   * registered user through onboarding again. Found in review alongside the toggle
   * bug; same c45 family, lower frequency.
   *
   * c385 KEPT THIS AND MOVED ITS CALLER. It used to hang off the email form's
   * "Back" button, which the one-page layout deletes. The footer mode toggle is now
   * the only way to walk away from a pending wait, so it inherits the sign-out -
   * dropping this along with the Back button would have quietly reintroduced the
   * exact bug the comment above describes.
   */
  const abandonPendingSession = () => {
    if (submittedMode !== null && hasFirebaseConfig()) {
      void signOutUser().catch(() => {
        // Nothing useful to tell the user — they asked to leave and they are
        // leaving. SessionProvider's listener owns the state either way.
      });
    }
    setSubmitting(false);
    setSubmittedMode(null);
  };

  /**
   * c320: a failed attempt's error must not outlive the user's response to it.
   * Before this, the message was cleared only by a full form reset or the next
   * submit, so it sat there through further typing (reading as "still wrong"
   * over a corrected form) and through a mode switch (where it could be copy
   * about the OTHER mode — getAuthErrorMessage words some codes per mode).
   * The next keystroke or a mode flip means "I'm trying something different":
   * both clear it.
   */
  const editEmail = (value: string) => {
    setError(null);
    setEmail(value);
  };
  const editPassword = (value: string) => {
    setError(null);
    setPassword(value);
  };
  const editRepeatPassword = (value: string) => {
    setError(null);
    setRepeatPassword(value);
  };
  const toggleAuthMode = () => {
    // Order matters: abandon first, because it is the thing that can be waiting on
    // a credential, and the rest of this is just clearing UI state.
    abandonPendingSession();
    setError(null);
    setSocialError(null);
    setResetNotice(null);
    // Never carry a repeat-password across a mode switch. Left behind, it would be
    // compared against a password typed afterwards.
    setRepeatPassword("");
    setShowPassword(false);
    setShowRepeatPassword(false);
    setAuthMode(authMode === "signin" ? "signup" : "signin");
  };

  /**
   * Password reset (c385). The reference shot asks for this link and the app had no
   * recovery path at all, so it is a real sendPasswordResetEmail rather than a
   * decorative link.
   *
   * THE SAME NOTICE IS SHOWN WHETHER OR NOT THE ADDRESS HAS AN ACCOUNT. Firebase
   * throws auth/user-not-found and surfacing it would turn this into an
   * account-enumeration oracle for any address someone cares to type - the same
   * reasoning that already collapses user-not-found and wrong-password into one
   * message on sign-in (src/auth/authErrors.ts). Every OTHER code is reported
   * normally: auth/invalid-email is about the text they typed, not about who is
   * registered, and silently swallowing it would leave a malformed address looking
   * like a sent email.
   */
  const handleForgotPassword = async () => {
    const address = email.trim();
    setResetNotice(null);
    if (address.length === 0) {
      setError("Enter your email address first, then choose Forgot password.");
      return;
    }
    if (!hasFirebaseConfig()) {
      setResetNotice("Demo mode. No reset email was sent.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await sendPasswordReset(address);
    } catch (err) {
      if (!isUserNotFoundError(err)) {
        setError(getAuthErrorMessage(err, "signin"));
        setSubmitting(false);
        return;
      }
    }
    setSubmitting(false);
    setResetNotice("If that address has an account, a reset link is on its way.");
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

    // c385, sign-up only. Checked AFTER length so somebody who typed the same too
    // short password twice is told the useful thing ("at least 6 characters")
    // rather than being sent to fix a mismatch that isn't there. Purely
    // client-side: Firebase has no concept of a confirmation field, so this exists
    // to catch a typo in a value the user cannot see, which is the whole reason the
    // reference has the field.
    if (mode === "signup" && password !== repeatPassword) {
      setError("Those passwords don't match.");
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

  const canSubmit =
    email.trim().length > 0 &&
    password.length > 0 &&
    // Non-EMPTY only, not matching: an empty confirmation is an unfinished form, but
    // a mismatched one is a mistake the user deserves to be told about, and a
    // disabled button says nothing. The match check lives in submitEmailForm.
    (authMode === "signin" || repeatPassword.length > 0) &&
    !submitting;

  const isSignUp = authMode === "signup";
  // Links sit on the bare canvas with no button behind them, so `accent` is not
  // automatically legible - on the dark canvas a campus-primary accent (UNCG navy)
  // measures about 1.2:1. canvasActionColor measures and falls back to the campus
  // secondary. See its comment in src/theme.
  const linkColor = canvasActionColor(palette, campusColors);

  return (
    /* `fillHeight` makes the content grow to the viewport so the spacer below can
       push the footer down (braul: "too much white space on the bottom"). It is
       ALSO what makes the `flex: 1` on this View legal - without flexGrow on the
       scroll container a flex child collapses to zero height on native and this
       screen renders as a bare canvas, which is exactly what it did before c385's
       device pass. The two go together; do not remove one and keep the other. */
    <Screen scroll fillHeight>
      <View style={{ flex: 1, gap: spacing.xl, paddingTop: spacing.xxl }}>
        {/* The title IS the brand moment now (DESIGN section 7, c385) - it replaced
            an accentGradient HeroCard wordmark that was a block of chrome sitting
            above the screen's actual job. */}
        <View style={{ gap: spacing.xs }}>
          <AppText variant="display">
            {isSignUp ? "Welcome to Chirp" : "Welcome back"}
          </AppText>
          <AppText variant="caption" tone="secondary">
            {isSignUp ? "Create your account" : "Sign in to your account"}
          </AppText>
        </View>

        <View style={{ gap: spacing.lg }}>
          <UnderlineField
            label="E-mail"
            value={email}
            onChangeText={editEmail}
            placeholder="you@school.edu"
            keyboardType="email-address"
            autoCapitalize="none"
            autoCorrect={false}
            textContentType="emailAddress"
            // Decorative, not a control: there is nothing to do with an at-sign, and
            // an icon with no action must not announce itself as a button.
            icon="at-sign"
          />

          <UnderlineField
            label="Password"
            value={password}
            onChangeText={editPassword}
            placeholder="Your password"
            secureTextEntry={!showPassword}
            autoCapitalize="none"
            autoCorrect={false}
            textContentType={isSignUp ? "newPassword" : "password"}
            // The hint is only true on sign-up: MIN_PASSWORD_LENGTH is Firebase's
            // floor for CREATING an account, and telling a returning user their real
            // password needs 6 characters would be both wrong and alarming.
            hint={isSignUp ? `At least ${MIN_PASSWORD_LENGTH} characters` : undefined}
            action={{
              icon: showPassword ? "eye-off" : "eye",
              label: showPassword ? "Hide password" : "Show password",
              onPress: () => setShowPassword(!showPassword),
            }}
          />

          {isSignUp ? (
            <UnderlineField
              label="Repeat password"
              value={repeatPassword}
              onChangeText={editRepeatPassword}
              placeholder="Type it again"
              secureTextEntry={!showRepeatPassword}
              autoCapitalize="none"
              autoCorrect={false}
              textContentType="newPassword"
              action={{
                icon: showRepeatPassword ? "eye-off" : "eye",
                label: showRepeatPassword ? "Hide repeated password" : "Show repeated password",
                onPress: () => setShowRepeatPassword(!showRepeatPassword),
              }}
            />
          ) : null}
        </View>

        {error !== null ? (
          <AppText variant="caption" tone="danger">
            {error}
          </AppText>
        ) : null}

        {resetNotice !== null ? (
          <AppText variant="caption" tone="secondary">
            {resetNotice}
          </AppText>
        ) : null}

        {!hasFirebaseConfig() ? (
          <AppText variant="caption" tone="tertiary">
            Demo mode. Firebase not configured
          </AppText>
        ) : null}

        {/* Sign-in only. The reference puts "Forgot passward" on its create-account
            screen, where it means nothing: there is no password to have forgotten
            yet. */}
        {!isSignUp ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Forgot password"
            disabled={submitting}
            onPress={() => void handleForgotPassword()}
            hitSlop={spacing.sm}
            style={{ alignSelf: "flex-end", opacity: submitting ? 0.4 : 1 }}
          >
            <AppText variant="caption" style={{ color: linkColor }}>
              Forgot password?
            </AppText>
          </Pressable>
        ) : null}

        {/* Slack ABOVE the CTA as well as below the social block, so the two spacers
            split the leftover height evenly and the CTA-through-Google band sits
            lower on the screen instead of riding directly under the fields (braul,
            Sep 8). Both collapse to nothing the moment the content is tall enough to
            scroll, so a small phone and a keyboard-up layout are unaffected. */}
        <View style={{ flex: 1, minHeight: spacing.lg }} />

        <Button
          // The screen's one gold moment (DESIGN section 10.4 rule 4): solid accent
          // fill, campus-secondary label. Same pairing the tab bar ships.
          variant="brand"
          label={submitting ? "Please wait..." : isSignUp ? "Create an account" : "Sign in"}
          disabled={!canSubmit}
          onPress={() => void submitEmailForm()}
        />

        <View style={{ gap: spacing.md }}>
          <AppText variant="caption" tone="secondary" style={{ textAlign: "center" }}>
            {isSignUp ? "Or sign up with" : "Or sign in with"}
          </AppText>
          {/* TWO providers, not the reference's three: Chirp has Apple and Google,
              and Instagram is not a provider this app has. A third button that
              cannot authenticate is exactly what handleUnavailableSocialProvider
              above exists to prevent.
              Text labels rather than the reference's icon-only circles: Feather has
              no brand marks, DESIGN forbids mixing icon families, and Apple and
              Google both require their own official marks for sign-in buttons. */}
          <View style={{ flexDirection: "row", gap: spacing.md }}>
            {/* `neutral`, NOT `secondary` (c385). Caught by rendering this screen in
                dark mode, where both buttons were all but invisible: secondary is
                accentSoft fill + accent label, and with the default campus-primary
                accent that measures 1.18:1 on the dark canvas. These are alternative
                routes in, not accent moments, so a real neutral surface is also the
                right semantics. The wider secondary-in-dark defect is board c386. */}
            <Button
              label="Apple"
              variant="neutral"
              disabled={submitting}
              onPress={handleApplePress}
              style={{ flex: 1 }}
            />
            <Button
              label="Google"
              variant="neutral"
              disabled={submitting}
              onPress={handleGooglePress}
              style={{ flex: 1 }}
            />
          </View>
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
        </View>

        {/* The other half of the pair above: keeps the footer and legal line at the
            bottom rather than letting them float up under the social buttons. */}
        <View style={{ flex: 1, minHeight: spacing.lg }} />

        {/* The reference's own footer says "Don't have an account? Sign Up" on a
            screen titled "Create your account", which is self-contradictory. This one
            reflects the mode actually showing. */}
        <Pressable
          accessibilityRole="button"
          disabled={submitting}
          onPress={toggleAuthMode}
          hitSlop={spacing.sm}
          style={{
            flexDirection: "row",
            justifyContent: "center",
            gap: spacing.xs,
            opacity: submitting ? 0.4 : 1,
          }}
        >
          <AppText variant="caption" tone="secondary">
            {isSignUp ? "Already have an account?" : "Don't have an account?"}
          </AppText>
          <AppText variant="caption" style={{ color: linkColor, fontWeight: "700" }}>
            {isSignUp ? "Sign in" : "Sign up"}
          </AppText>
        </Pressable>

        <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
          By continuing, you agree to Chirp's Terms of Service and acknowledge our Privacy Policy.
        </AppText>
      </View>
    </Screen>
  );
}
