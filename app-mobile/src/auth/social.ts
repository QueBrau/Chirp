/**
 * Social sign-in capability declaration (c89).
 *
 * Apple and Google are intentionally unavailable until their native providers
 * are configured in Firebase and the iOS/Android dev builds.  Keeping that
 * state in one small, provider-agnostic module gives the sign-in screen an
 * honest guard today and leaves one seam for the real credential exchange
 * later.  This module must never manufacture a credential or route a user
 * into onboarding: an unavailable provider is not an authenticated session.
 */

export type SocialAuthProvider = "apple" | "google";

export interface SocialAuthStatus {
  provider: SocialAuthProvider;
  enabled: false;
  reason: string;
}

const SOCIAL_AUTH_STATUS: Record<SocialAuthProvider, SocialAuthStatus> = {
  apple: {
    provider: "apple",
    enabled: false,
    reason: "Sign in with Apple is not configured in the native build yet.",
  },
  google: {
    provider: "google",
    enabled: false,
    reason: "Google sign-in is not configured in the native build yet.",
  },
};

/** Return the current capability without attempting any provider work. */
export function getSocialAuthStatus(provider: SocialAuthProvider): SocialAuthStatus {
  return SOCIAL_AUTH_STATUS[provider];
}

/** User-facing copy for a provider that cannot authenticate this build. */
export function socialAuthUnavailableMessage(provider: SocialAuthProvider): string {
  const label = provider === "apple" ? "Apple" : "Google";
  const status = getSocialAuthStatus(provider);
  return `${label} sign-in isn't available in this build yet. ${status.reason} Continue with Email instead.`;
}
