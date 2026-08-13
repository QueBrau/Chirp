/**
 * Firebase project config — PLACEHOLDER VALUES ONLY. No Firebase project exists yet
 * (see /SETUP-FIREBASE.md at the repo root for the console steps that create one).
 *
 * Replace every field below with the real values from Firebase console → Project
 * settings → General → "Your apps" (web app) once the project + apps are
 * registered. Until every field is replaced, hasFirebaseConfig() returns false and
 * the app runs in mock/demo auth mode (see sign-in.tsx).
 */

export interface FirebaseConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket: string;
  messagingSenderId: string;
  appId: string;
}

// TODO(milestone-1, real Firebase project): replace every "REPLACE_ME_*" value
// below with the real config from the Firebase console (see SETUP-FIREBASE.md).
export const firebaseConfig: FirebaseConfig = {
  apiKey: "REPLACE_ME_FIREBASE_API_KEY",
  authDomain: "REPLACE_ME.firebaseapp.com",
  projectId: "REPLACE_ME_PROJECT_ID",
  storageBucket: "REPLACE_ME.appspot.com",
  messagingSenderId: "REPLACE_ME_SENDER_ID",
  appId: "REPLACE_ME_APP_ID",
};

const PLACEHOLDER_PREFIX = "REPLACE_ME";

/**
 * True once every field in firebaseConfig has been replaced with a real value.
 * Gate on this before touching src/auth/firebase.ts or src/auth/session.ts —
 * both throw if called while this is false.
 */
export function hasFirebaseConfig(): boolean {
  return Object.values(firebaseConfig).every(
    (value) => value.length > 0 && !value.startsWith(PLACEHOLDER_PREFIX)
  );
}
