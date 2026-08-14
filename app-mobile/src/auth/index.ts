/** Auth barrel: Firebase config guard, app/auth instance, session helpers, session context. */

export { firebaseConfig, hasFirebaseConfig, type FirebaseConfig } from "./config";
export { getFirebaseAuth } from "./firebase";
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
  type SessionContextValue,
  type SessionStatus,
} from "./SessionProvider";
