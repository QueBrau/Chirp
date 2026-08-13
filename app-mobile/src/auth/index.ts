/** Auth barrel: Firebase config guard, app/auth instance, and session helpers. */

export { firebaseConfig, hasFirebaseConfig, type FirebaseConfig } from "./config";
export { getFirebaseAuth } from "./firebase";
export {
  getIdToken,
  onAuthChanged,
  signInWithEmail,
  signOutUser,
  signUpWithEmail,
} from "./session";
