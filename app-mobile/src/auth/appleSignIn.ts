/**
 * Native "Sign in with Apple" credential exchange (board c314).
 *
 * src/auth/social.ts stays the honest capability DECLARATION for both
 * providers - it must keep saying Apple is "not configured" wherever this
 * flow genuinely cannot run, and it never imports a native module. This file
 * is deliberately a separate sibling rather than folded into social.ts so
 * that boundary stays legible: one file is prose the sign-in screen reads to
 * decide what to say, the other is the real credential exchange.
 *
 * Same contract as src/auth/session.ts: every function here assumes its
 * precondition was already checked by the caller (there, hasFirebaseConfig();
 * here, isAppleSignInAvailable()) rather than re-checking internally. On a
 * successful exchange the ID token is handed to src/api/client's
 * setAuthToken() exactly the way signInWithEmail/signUpWithEmail do it - the
 * API client has no other way to learn a session exists. Per c89, an
 * unavailable or failed provider must never look like an authenticated
 * session: every exit that is not a genuine Firebase credential returns
 * "cancelled" or "error", never "success".
 */

import * as AppleAuthentication from "expo-apple-authentication";
import * as Crypto from "expo-crypto";
import { OAuthProvider, signInWithCredential } from "firebase/auth";
import { Platform } from "react-native";

import { setAuthToken } from "@/api/client";

import { getFirebaseAuth } from "./firebase";

/**
 * True only on iOS AND when the OS itself reports Apple authentication is
 * supported (expo-apple-authentication's own runtime check - this is false on
 * a simulator/OS version without it, and false on every non-iOS platform).
 * Callers must check this BEFORE calling signInWithApple() and fall back to
 * social.ts's honest-stub copy when it is false - this module does not
 * re-check, matching the precondition convention above.
 */
export async function isAppleSignInAvailable(): Promise<boolean> {
  if (Platform.OS !== "ios") return false;
  return AppleAuthentication.isAvailableAsync();
}

export type AppleSignInOutcome =
  | { status: "success" }
  | { status: "cancelled" }
  | { status: "error"; message: string };

/**
 * A cryptographically random nonce, hex-encoded so it is also a valid string
 * to hash. Kept in this exact string form because the SAME string is used
 * twice below in two different forms - see signInWithApple().
 */
function generateRawNonce(): string {
  const bytes = Crypto.getRandomBytes(32);
  return Array.from(bytes)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Human copy for an Apple sign-in failure. Never a raw Firebase/Apple error
 * code - c311 established that principle for email and it applies here too.
 *
 * Deliberately NOT routed through authErrors.ts's getAuthErrorMessage(): that
 * mapper sends auth/operation-not-allowed to "Email sign-in isn't available
 * right now", which is a false statement about email when the actual failure
 * is Apple's provider not being turned on in the Firebase console (true today
 * - c314 ships the native flow ahead of that console step). Rather than add a
 * provider/mode parameter to a mapper that c311 wrote and reviewed specifically
 * for the two email codes that need account-enumeration-safe collapsing, Apple
 * gets its own small mapper here. The two mappers share no state, so this
 * mapper drifting from authErrors.ts's wording for a shared code (e.g.
 * network-request-failed) is a copy-review concern, not a correctness one.
 */
function appleErrorMessage(error: unknown): string {
  const code = readErrorCode(error);

  switch (code) {
    case "auth/operation-not-allowed":
      // The Apple OAuth provider itself is not enabled in the Firebase
      // console - this is the current state of the project.
      return "Sign in with Apple isn't available right now. Try again later or use Email.";
    case "auth/network-request-failed":
      return "Check your internet connection and try again.";
    case "auth/user-disabled":
      return "This account has been disabled. Contact support if you think that's a mistake.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes and try again.";
    default:
      return "Sign in with Apple didn't work. Try again or use Email.";
  }
}

/** Reads a `.code` string off an unknown thrown value, defensively. */
function readErrorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

/**
 * Runs the native Apple sign-in flow and, on success, exchanges the resulting
 * Apple identity token for a Firebase session.
 *
 * Callers MUST have already confirmed isAppleSignInAvailable() before calling
 * this - see the module doc comment.
 */
export async function signInWithApple(): Promise<AppleSignInOutcome> {
  try {
    // The nonce is generated once and used in two DIFFERENT forms below: Apple
    // receives its SHA-256 hash, Firebase receives the raw value. Sending the
    // hash to Firebase (or the raw value to Apple) is the single most common
    // way this flow gets implemented backwards, and it fails with an opaque
    // error - hence two distinctly-named variables instead of one reused nonce.
    const rawNonce = generateRawNonce();
    const hashedNonce = await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, rawNonce);

    let appleCredential: AppleAuthentication.AppleAuthenticationCredential;
    try {
      appleCredential = await AppleAuthentication.signInAsync({
        requestedScopes: [
          AppleAuthentication.AppleAuthenticationScope.FULL_NAME,
          AppleAuthentication.AppleAuthenticationScope.EMAIL,
        ],
        nonce: hashedNonce, // Apple gets the HASH. Never the raw nonce - see above.
      });
    } catch (error) {
      // expo-apple-authentication rejects with this exact code when the user
      // dismisses the system sheet (verified against
      // node_modules/expo-apple-authentication/ios/AppleAuthenticationExceptions.swift
      // and its own AppleAuthentication.d.ts doc comment). That is NOT an
      // error: show nothing, no error text, no alert.
      if (readErrorCode(error) === "ERR_REQUEST_CANCELED") {
        return { status: "cancelled" };
      }
      return { status: "error", message: appleErrorMessage(error) };
    }

    if (!appleCredential.identityToken) {
      // Apple returned no token. Never proceed without one - c89's "an
      // unavailable provider is not an authenticated session" applies here too.
      return { status: "error", message: "Sign in with Apple didn't work. Try again or use Email." };
    }

    const credential = new OAuthProvider("apple.com").credential({
      idToken: appleCredential.identityToken,
      rawNonce, // Firebase gets the RAW value. Never the hash - see above.
    });
    const userCredential = await signInWithCredential(getFirebaseAuth(), credential);
    // Same token handoff as signInWithEmail/signUpWithEmail in session.ts -
    // the API client has no other way to learn this session exists.
    setAuthToken(await userCredential.user.getIdToken());
    return { status: "success" };
  } catch (error) {
    // Outer net: nonce generation/hashing, the Firebase credential exchange,
    // or getIdToken() can all throw too, and the caller (sign-in.tsx) must
    // always get a settled outcome rather than an unhandled rejection that
    // would leave the screen stuck on "Please wait..." forever.
    return { status: "error", message: appleErrorMessage(error) };
  }
}
