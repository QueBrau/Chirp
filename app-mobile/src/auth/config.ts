/**
 * Firebase project config — LIVE values for the chirps-prod project (web app
 * "chirp-web"). This is public client config, not a secret (Firebase web config
 * is designed to ship in the client; access is guarded by Firebase Auth rules) —
 * see SETUP-FIREBASE.md §4. Email/Password is the enabled provider; Google/Apple
 * are console follow-ups.
 */

export interface FirebaseConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket: string;
  messagingSenderId: string;
  appId: string;
}

export const firebaseConfig: FirebaseConfig = {
  apiKey: "AIzaSyC41tPPMCQjpbUbjJSufxp4GW-rTg_LTNk",
  authDomain: "chirps-prod.firebaseapp.com",
  projectId: "chirps-prod",
  storageBucket: "chirps-prod.firebasestorage.app",
  messagingSenderId: "593616178468",
  appId: "1:593616178468:web:a3ef5fbbbe161d0fbf37b1",
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
