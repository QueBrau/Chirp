/** Auth barrel: Firebase config guard, app/auth instance, session helpers, session context. */

export { firebaseConfig, hasFirebaseConfig, type FirebaseConfig } from "./config";
export { getFirebaseAuth } from "./firebase";
export {
  getAuthErrorMessage,
  getPasswordLengthError,
  type AuthErrorMode,
} from "./authErrors";
export {
  getIdToken,
  onAuthChanged,
  onIdTokenChanged,
  signInWithEmail,
  signOutUser,
  signUpWithEmail,
} from "./session";
export {
  SessionProvider,
  useSession,
} from "./SessionProvider";
export { withInviteCode, inviteShareUrl, WEB_BASE_URL } from "./inviteLink";
export { useCampus } from "./useCampus";
export { useCampusAccess, type CampusAccess } from "./useCampusAccess";
export {
  getSocialAuthStatus,
  socialAuthUnavailableMessage,
  type SocialAuthProvider,
  type SocialAuthStatus,
} from "./social";
export {
  isAppleSignInAvailable,
  signInWithApple,
  type AppleSignInOutcome,
} from "./appleSignIn";
export {
  type SessionContextValue,
  type SessionStatus,
} from "./SessionProvider";
