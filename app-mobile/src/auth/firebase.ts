/**
 * Firebase app + auth instance, initialized lazily and ONLY when hasFirebaseConfig()
 * is true (src/auth/config.ts). Importing this module must never throw or touch the
 * network while running in mock/demo mode — initialization happens on first call to
 * getFirebaseAuth(), not at module load.
 *
 * Persistence is platform-aware (c166):
 *   - web: getAuth(app), unchanged — the browser SDK wires its own persistence
 *     (indexedDB/localStorage) internally, and this branch must keep calling
 *     getAuth exactly as before. Do not swap it for initializeAuth.
 *   - native (ios/android): initializeAuth(app, { persistence:
 *     getReactNativePersistence(AsyncStorage) }), so a signed-in session survives
 *     the app being killed. Before this fix, native had NO persistence — Metro
 *     warned on every launch and a real signed-in student was signed out on every
 *     app restart (masked in dev by EXPO_PUBLIC_DEV_UID recreating the session).
 *
 * getReactNativePersistence RUNTIME resolution: Metro resolves "firebase/auth" ->
 * "@firebase/auth"'s own exports map, whose "react-native" condition (active for
 * ios/android per @expo/metro-config's unstable_conditionsByPlatform, matched
 * against expo/tsconfig.base's customConditions) points at dist/rn/index.js, which
 * genuinely exports getReactNativePersistence (confirmed by reading
 * node_modules/@firebase/auth/dist/rn/index.js directly).
 *
 * getReactNativePersistence TYPES: unreachable via the normal import, and that is
 * an @firebase/auth packaging quirk, not a version problem. Both firebase's root
 * package.json ("./auth" export) and @firebase/auth's own package.json list a
 * bare "types" condition BEFORE "react-native" in their exports map for ".". TS's
 * bundler resolution always treats "types" as active, so key order — not
 * customConditions — decides the match, and "types" wins first every time,
 * pointing at the combined public d.ts (dist/auth-public.d.ts /
 * dist/rn/index.rn.d.ts is never reached). That combined d.ts declares
 * initializeAuth and getAuth but omits this RN-only symbol. No alternate
 * subpath exposes better types — @firebase/auth's own "." export has the same
 * "types"-before-"react-native" ordering, so importing from "@firebase/auth"
 * directly would hit the identical gap. Hence the scoped suppression below
 * instead of a subpath swap.
 */

import { Platform } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { getApps, initializeApp, type FirebaseApp } from "firebase/app";
import {
  getAuth,
  initializeAuth,
  // @ts-expect-error — getReactNativePersistence exists at runtime (verified in
  // node_modules/@firebase/auth/dist/rn/index.js) but @firebase/auth's exports
  // map resolves "firebase/auth" types to the combined public d.ts (see file
  // header), which does not declare this RN-only symbol. Remove this suppression
  // once an @firebase/auth release fixes the exports-map ordering; if this line
  // stops erroring, tsc will fail the build and tell us.
  getReactNativePersistence,
  type Auth,
} from "firebase/auth";

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
    const firebaseApp = getFirebaseApp();
    auth =
      Platform.OS === "web"
        ? getAuth(firebaseApp)
        : initializeAuth(firebaseApp, {
            persistence: getReactNativePersistence(AsyncStorage),
          });
  }
  return auth;
}
