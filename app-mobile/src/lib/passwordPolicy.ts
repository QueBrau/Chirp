/**
 * Password length policy for email/password sign-up (board c311).
 *
 * Deliberately its own file rather than folded into src/lib/contentLimits.ts:
 * that file's whole purpose - and its `verify:content-limits` CI check - is
 * mirroring caps that live in backend/app/core/validation.py, matched by scanning
 * for `export const MAX_*` numeric literals and failing if the backend disagrees
 * or has no such cap at all. Password length has no backend counterpart - only
 * Firebase enforces it - so a MAX_PASSWORD_LENGTH living in that file would trip
 * that check as a phantom unmirrored cap. This file borrows contentLimits.ts's
 * shape (named numeric export, one per line, doc comment above each) without
 * corrupting the one invariant that file exists to guard.
 */

/**
 * Firebase's own server-side MINIMUM for createUserWithEmailAndPassword - fall
 * below this and Firebase itself throws auth/weak-password. Matched EXACTLY.
 * Do not raise it: a stricter client minimum would reject passwords Firebase
 * would happily accept, and the rejection copy would be lying about why.
 */
export const MIN_PASSWORD_LENGTH = 6;

/**
 * Firebase has no practical maximum password length, so this ceiling is OUR OWN
 * choice, not a mirror of any server rule. Set generously per NIST SP 800-63B
 * guidance (allow long passphrases; 64 is the floor, not a target) so a student
 * using a real passphrase never gets turned away. If someone is ever tempted to
 * tighten this to match "the server limit" - there isn't one. Don't invent one.
 */
export const MAX_PASSWORD_LENGTH = 64;

/** True when a sign-up password is shorter than Firebase will accept. */
export function isPasswordTooShort(password: string): boolean {
  return password.length < MIN_PASSWORD_LENGTH;
}

/** True when a sign-up password is longer than our own generous cap. */
export function isPasswordTooLong(password: string): boolean {
  return password.length > MAX_PASSWORD_LENGTH;
}
