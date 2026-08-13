/**
 * Firebase app + auth instance, initialized lazily and ONLY when hasFirebaseConfig()
 * is true (src/auth/config.ts). Importing this module must never throw or touch the
 * network while running in mock/demo mode — initialization happens on first call to
 * getFirebaseAuth(), not at module load.
 *
 * No React Native persistence layer (@react-native-async-storage/async-storage +
 * getReactNativePersistence) is wired yet — that's dev-build/native-config work for
 * when the real project lands (see /SETUP-FIREBASE.md). Sessions won't survive an
 * app restart until then.
 */

import { getApps, initializeApp, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";

import { firebaseConfig, hasFirebaseConfig } from "./config";

let app: FirebaseApp | null = null;
let auth: Auth | null = null;

function getFirebaseApp(): FirebaseApp {
  if (!hasFirebaseConfig()) {
    throw new Error(
      "Firebase is not configured (see SETUP-FIREBASE.md) — use the mock auth flow instead of calling into src/auth/session.ts."
    );
  }
  if (!app) {
    app = getApps().length > 0 ? getApps()[0]! : initializeApp(firebaseConfig);
  }
  return app;
}

/** Shared Auth instance, initialized on first call. Throws if Firebase isn't configured. */
export function getFirebaseAuth(): Auth {
  if (!auth) {
    auth = getAuth(getFirebaseApp());
  }
  return auth;
}
