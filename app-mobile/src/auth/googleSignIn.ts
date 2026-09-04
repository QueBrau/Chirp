/**
 * Native Google sign-in credential exchange (board c169).
 *
 * Deliberately a sibling of src/auth/appleSignIn.ts (c314) rather than a
 * merged "social" module, and for the same reason that file is a sibling of
 * social.ts: social.ts stays the honest capability DECLARATION that never
 * imports a native module, and each provider's real credential exchange lives
 * in its own file. Same contract as session.ts/appleSignIn.ts: every function
 * assumes its precondition (here, isGoogleSignInAvailable()) was checked by
 * the caller. Per c89, an unavailable or failed provider must never look like
 * an authenticated session: every exit that is not a genuine Firebase
 * credential returns "cancelled" or "error", never "success".
 *
 * On success the Firebase ID token is handed to src/api/client's
 * setAuthToken() exactly the way signInWithEmail/signUpWithEmail and
 * signInWithApple do it — the API client has no other way to learn a session
 * exists. SessionProvider's onAuthStateChanged listener then resolves the
 * account against GET /auth/me the same as every other provider (verified for
 * c169: nothing in that path is provider-specific).
 */

import {
  GoogleSignin,
  statusCodes,
} from "@react-native-google-signin/google-signin";
import { GoogleAuthProvider, signInWithCredential } from "firebase/auth";
import { Platform } from "react-native";

import { setAuthToken } from "@/api/client";

import { hasFirebaseConfig } from "./config";
import { getFirebaseAuth } from "./firebase";

/**
 * OAuth client ids for the chirps-prod Google provider. Like firebaseConfig
 * in src/auth/config.ts these are PUBLIC client config, designed to ship in
 * the app binary — the web id was minted by enabling the Google provider in
 * the Firebase console (Sep 4), and the iOS id comes from a Google Cloud
 * console "iOS" OAuth client bound to bundle id app.chirps.mobile. The web id
 * is what makes GoogleSignin.signIn() return an ID token Firebase will
 * accept; the iOS id (plus its reversed form as `iosUrlScheme` in app.json's
 * @react-native-google-signin config plugin) is what lets the native sheet
 * return to the app. The REPLACE_ME convention matches config.ts: until the
 * real iOS id lands, isGoogleSignInAvailable() stays false and the sign-in
 * screen keeps c89's honest stub copy.
 */
const GOOGLE_WEB_CLIENT_ID =
  "593616178468-dltvkkric6o3svoc8psmkjo6ka4ki7qa.apps.googleusercontent.com";
const GOOGLE_IOS_CLIENT_ID =
  "593616178468-m5ai5f72q8jjpijv18o3hsec27vef95d.apps.googleusercontent.com";

const PLACEHOLDER_PREFIX = "REPLACE_ME";

function hasGoogleSignInConfig(): boolean {
  return (
    !GOOGLE_WEB_CLIENT_ID.startsWith(PLACEHOLDER_PREFIX) &&
    !GOOGLE_IOS_CLIENT_ID.startsWith(PLACEHOLDER_PREFIX)
  );
}

/**
 * True only on iOS with both Firebase and the Google OAuth clients configured.
 * Deliberately iOS-only for now, matching c314's Apple scope: an Android build
 * would additionally need its SHA-1 fingerprint registered in the console
 * (see the provider's own console warning), which no card has done — saying
 * "available" there would put a live-looking button in front of a flow that
 * can only fail. Unlike isAppleSignInAvailable() this needs no async OS query,
 * so it is synchronous — callers can use it inline.
 */
export function isGoogleSignInAvailable(): boolean {
  return Platform.OS === "ios" && hasFirebaseConfig() && hasGoogleSignInConfig();
}

export type GoogleSignInOutcome =
  | { status: "success" }
  | { status: "cancelled" }
  | { status: "error"; message: string };

/**
 * GoogleSignin.configure() must run before the first signIn() call. It is
 * synchronous, idempotent for our fixed config, and cheap — but keeping it
 * behind a flag makes the "configure exactly once, lazily" intent legible and
 * keeps module import free of side effects (same rule as firebase.ts: nothing
 * here may touch native state at import time).
 */
let configured = false;

function configureOnce(): void {
  if (configured) return;
  GoogleSignin.configure({
    webClientId: GOOGLE_WEB_CLIENT_ID,
    iosClientId: GOOGLE_IOS_CLIENT_ID,
  });
  configured = true;
}

/**
 * Human copy for a Google sign-in failure. Never a raw error code, and never
 * routed through authErrors.ts's email mapper — same reasoning as
 * appleSignIn.ts's own mapper: c311 wrote that one specifically for the email
 * codes that need account-enumeration-safe collapsing, and it renders
 * auth/operation-not-allowed as a sentence about EMAIL being unavailable,
 * which is a false statement when the failing provider is Google.
 */
function googleErrorMessage(error: unknown): string {
  const code = readErrorCode(error);

  switch (code) {
    case "auth/operation-not-allowed":
      return "Google sign-in isn't available right now. Try again later or use Email.";
    case "auth/network-request-failed":
      return "Check your internet connection and try again.";
    case "auth/user-disabled":
      return "This account has been disabled. Contact support if you think that's a mistake.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes and try again.";
    case statusCodes.IN_PROGRESS:
      return "A Google sign-in is already in progress. Give it a moment.";
    case statusCodes.PLAY_SERVICES_NOT_AVAILABLE:
      // Android-only; unreachable while isGoogleSignInAvailable() is iOS-only,
      // but the mapper stays total rather than assuming that never changes.
      return "Google Play services isn't available on this device. Use Email instead.";
    default:
      return "Google sign-in didn't work. Try again or use Email.";
  }
}

/** Reads a `.code` string off an unknown thrown value, defensively. */
function readErrorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

/**
 * Runs the native Google sign-in sheet and, on success, exchanges the
 * resulting Google ID token for a Firebase session.
 *
 * Callers MUST have already confirmed isGoogleSignInAvailable() — see the
 * module doc comment.
 */
export async function signInWithGoogle(): Promise<GoogleSignInOutcome> {
  try {
    configureOnce();

    let idToken: string | null;
    try {
      // v16's Original API reports the user dismissing the sheet as a
      // RESPONSE ({type:"cancelled"}), not a rejection (verified against
      // node_modules/@react-native-google-signin/google-signin/lib/typescript/
      // src/signIn/GoogleSignin.d.ts and types.d.ts). The catch below still
      // maps a thrown SIGN_IN_CANCELLED code the same way in case an older
      // native path surfaces it — either shape of cancel is a silent no-op
      // per c89/c314, never an error alert.
      const response = await GoogleSignin.signIn();
      if (response.type === "cancelled") {
        return { status: "cancelled" };
      }
      idToken = response.data.idToken;
    } catch (error) {
      if (readErrorCode(error) === statusCodes.SIGN_IN_CANCELLED) {
        return { status: "cancelled" };
      }
      return { status: "error", message: googleErrorMessage(error) };
    }

    if (!idToken) {
      // Google returned a user but no ID token (possible when configure()
      // lacks a valid webClientId). Never proceed without one — c89's "an
      // unavailable provider is not an authenticated session" applies here.
      return { status: "error", message: "Google sign-in didn't work. Try again or use Email." };
    }

    const credential = GoogleAuthProvider.credential(idToken);
    const userCredential = await signInWithCredential(getFirebaseAuth(), credential);
    // Same token handoff as signInWithEmail/signUpWithEmail in session.ts —
    // the API client has no other way to learn this session exists.
    setAuthToken(await userCredential.user.getIdToken());
    return { status: "success" };
  } catch (error) {
    // Outer net: the Firebase credential exchange or getIdToken() can throw
    // too, and the caller (sign-in.tsx) must always get a settled outcome
    // rather than an unhandled rejection that leaves the screen stuck on
    // "Please wait..." forever.
    return { status: "error", message: googleErrorMessage(error) };
  }
}
