/**
 * Human copy for Firebase Auth sign-in/sign-up failures (board c311).
 *
 * app/(auth)/sign-in.tsx used to catch with a bare `catch {}`, discarding the
 * thrown FirebaseError - and its `.code` - entirely. Every failure rendered the
 * same generic sentence: a too-short password, an already-registered email, a
 * malformed address, a rate-limit lockout and a dead network were all
 * indistinguishable. This module reads the code and returns copy specific to the
 * actual cause instead.
 *
 * Detecting a Firebase error is deliberately duck-typed: read a string `.code`
 * property rather than asserting `instanceof FirebaseError`. `instanceof` is
 * fragile across module duplication (two resolved copies of @firebase/util, e.g.
 * from a bad hoist, produce two distinct classes) and buys nothing a `.code`
 * check does not already give. Firebase's own type-doc comment for FirebaseError
 * shows the same `(e as FirebaseError)?.code` pattern for this reason.
 */

import { MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH } from "@/lib/passwordPolicy";

export type AuthErrorMode = "signin" | "signup";

/**
 * The fallback for anything this module does not recognise - a future Firebase
 * SDK bump adds a code, or something that is not a FirebaseError at all reaches
 * the catch. The SIGN-IN wording is preserved verbatim from what shipped before
 * c311, so an unrecognised error on that form degrades to exactly the sentence
 * that was already reviewed rather than a new, unreviewed one.
 *
 * MODE-AWARE on purpose. The single shared string was wrong half the time: a
 * failed ACCOUNT CREATION told the user we "couldn't sign you in", which is not
 * what they were doing. That is the same class of imprecision c311 exists to
 * fix, so the fallback has to get it right too.
 */
function genericMessage(mode: AuthErrorMode): string {
  return mode === "signup"
    ? "Couldn't create your account. Check your email and password and try again."
    : "Couldn't sign you in. Check your email and password and try again.";
}

/**
 * auth/user-not-found, auth/wrong-password and auth/invalid-credential all mean
 * one thing to an attacker probing the sign-in form: "try a different email" vs
 * "try a different password" is exactly the bit that reveals whether an address
 * has a Chirp account. That is account enumeration. Newer Firebase deliberately
 * collapses the first two into auth/invalid-credential server-side for this same
 * reason - this mapping stays collapsed even against an older SDK/project setting
 * that still returns the specific codes, so the client-side behavior does not
 * regress the server's own fix.
 *
 * DO NOT split this back out to name which one was wrong. That is the one place
 * in this module where more specific is worse, no matter how helpful it looks.
 */
const SIGNIN_INVALID_CREDENTIAL_MESSAGE =
  "That email and password don't match an account. Check both and try again.";

/**
 * auth/email-already-in-use is the mirror-image case: on SIGN-UP it must be
 * specific to be usable at all ("why won't this work" has exactly one honest
 * answer), and it leaks the same registration fact as above - unavoidably, since
 * the whole point of the message is telling the user the account exists. That
 * asymmetry - collapsed on sign-in, explicit on sign-up - is intended, not an
 * inconsistency to "fix" into matching each other.
 */
const SIGNUP_EMAIL_IN_USE_MESSAGE =
  "An account with that email already exists. Try signing in instead.";

/** Reads a Firebase-style `.code` off an unknown thrown value, defensively. */
function readErrorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

/**
 * Maps a caught sign-in/sign-up error to human copy. Never throws: reading the
 * code is wrapped so a hostile or unusual error shape (e.g. a getter that itself
 * throws) degrades to genericMessage(mode) instead of taking down the catch block that
 * called this.
 *
 * `mode` matters for exactly the two codes above - it decides whether the
 * account-enumeration-safe collapsed message applies (sign-in) or the specific
 * "email taken" message applies (sign-up). Every other code means the same thing
 * regardless of which form is showing.
 */
export function getAuthErrorMessage(error: unknown, mode: AuthErrorMode): string {
  let code: string | null;
  try {
    code = readErrorCode(error);
  } catch {
    return genericMessage(mode);
  }

  switch (code) {
    case "auth/invalid-email":
      return "That email address doesn't look right. Check it and try again.";

    case "auth/missing-password":
      return "Enter a password to continue.";

    case "auth/weak-password":
      return `Choose a password with at least ${MIN_PASSWORD_LENGTH} characters.`;

    case "auth/email-already-in-use":
      // Only reachable from createUserWithEmailAndPassword, but gate on mode
      // anyway rather than trust that - see the comment on the constant above.
      return mode === "signup" ? SIGNUP_EMAIL_IN_USE_MESSAGE : genericMessage(mode);

    case "auth/user-not-found":
    case "auth/wrong-password":
    case "auth/invalid-credential":
      // Only reachable from signInWithEmailAndPassword, but gate on mode anyway
      // rather than trust that - see the comment on the constant above.
      return mode === "signin" ? SIGNIN_INVALID_CREDENTIAL_MESSAGE : genericMessage(mode);

    case "auth/user-disabled":
      return "This account has been disabled. Contact support if you think that's a mistake.";

    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes and try again.";

    case "auth/network-request-failed":
      return "Check your internet connection and try again.";

    case "auth/operation-not-allowed":
      return "Email sign-in isn't available right now. Try again later.";

    default:
      return genericMessage(mode);
  }
}

/**
 * Client-side password length check for sign-up, run BEFORE the network call so
 * the obvious cases (way too short, a pasted-in novel) answer instantly instead
 * of waiting on a Firebase round trip. Returns null when the password is fine.
 *
 * Always null on sign-in - length is a sign-up-only concept. An existing
 * password already cleared this bar the day it was created, so judging it again
 * at sign-in would call a CORRECT password "invalid" whenever the real failure is
 * a mismatch, and it would leak our length policy to anyone probing the sign-in
 * form for free.
 */
export function getPasswordLengthError(password: string, mode: AuthErrorMode): string | null {
  if (mode !== "signup") return null;
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Choose a password with at least ${MIN_PASSWORD_LENGTH} characters.`;
  }
  if (password.length > MAX_PASSWORD_LENGTH) {
    return `Passwords can be at most ${MAX_PASSWORD_LENGTH} characters. Try a shorter one.`;
  }
  return null;
}
