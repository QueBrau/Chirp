/**
 * Firebase Auth session helpers — email/password only for now. Google/Apple Sign-In
 * need native config (expo-auth-session / expo-apple-authentication) that only works
 * in a dev build, not this Expo Go-less JS-only setup; see /SETUP-FIREBASE.md and the
 * caption on sign-in.tsx. Every function here requires hasFirebaseConfig() to be true
 * (src/auth/config.ts) — callers gate on it and fall back to the mock flow otherwise.
 *
 * On successful sign-in/sign-up/sign-out, the ID token is pushed into src/api/client's
 * setAuthToken() so subsequent API calls carry `Authorization: Bearer <idToken>`.
 */

import {
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  onIdTokenChanged as onFirebaseIdTokenChanged,
  signInWithEmailAndPassword,
  signOut,
  type User,
} from "firebase/auth";

import { setAuthToken } from "@/api/client";

import { getFirebaseAuth } from "./firebase";

/** Sign in an existing user with email/password. Throws on invalid credentials. */
export async function signInWithEmail(email: string, password: string): Promise<User> {
  const credential = await signInWithEmailAndPassword(getFirebaseAuth(), email, password);
  setAuthToken(await credential.user.getIdToken());
  return credential.user;
}

/** Create a new user with email/password. Throws if the email is already registered. */
export async function signUpWithEmail(email: string, password: string): Promise<User> {
  const credential = await createUserWithEmailAndPassword(getFirebaseAuth(), email, password);
  setAuthToken(await credential.user.getIdToken());
  return credential.user;
}

/** Sign out the current Firebase user and clear the API client's bearer token. */
export async function signOutUser(): Promise<void> {
  await signOut(getFirebaseAuth());
  setAuthToken(null);
}

/** Subscribe to Firebase auth state changes; returns the unsubscribe function. */
export function onAuthChanged(callback: (user: User | null) => void): () => void {
  return onAuthStateChanged(getFirebaseAuth(), callback);
}

/**
 * Subscribe to Firebase ID token changes — fires on sign-in, sign-out, AND the
 * silent background refresh Firebase performs roughly every hour (unlike
 * onAuthChanged above, which only fires on sign-in/out and misses the refresh).
 * Each change pushes the fresh token into src/api/client's setAuthToken(), or
 * clears it on sign-out, so requests never carry a stale ~1hr-expired token.
 * Returns the unsubscribe function. Call only when hasFirebaseConfig() is true.
 */
export function onIdTokenChanged(callback?: (user: User | null) => void): () => void {
  return onFirebaseIdTokenChanged(getFirebaseAuth(), async (user) => {
    setAuthToken(user ? await user.getIdToken() : null);
    callback?.(user);
  });
}

/** Current user's Firebase ID token, or null if signed out. Pass true to force a refresh. */
export async function getIdToken(forceRefresh = false): Promise<string | null> {
  const user = getFirebaseAuth().currentUser;
  if (!user) return null;
  return user.getIdToken(forceRefresh);
}
