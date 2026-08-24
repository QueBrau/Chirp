/**
 * Dev-only account impersonation for the seeded local stack (board card c159).
 *
 * Eight roles exist in the backend and, until this, six of them had never been
 * seen rendered — the app is hardwired to the chirps-prod Firebase project, so
 * looking at the treasurer view meant being a real treasurer of a real chapter in
 * production. This is the switch that makes each seeded account viewable:
 *
 *     http://localhost:8081/?uid=dev-treasurer
 *
 * The choice sticks: once set, a bare route like /feed keeps the same account.
 * `?uid=off` clears it and hands you back the real sign-in screen.
 *
 * NO PASSWORD IS INVOLVED. The uid is sent as `X-Debug-Firebase-Uid`, which the
 * backend only honours under AUTH_MODE=emulated.
 *
 * THREE INDEPENDENT LOCKS keep this out of production, and none of them relies on
 * remembering to take the switch out:
 *
 *   1. `__DEV__` is false in any release build, so this returns null and the whole
 *      path is dead before the header is ever set.
 *   2. The server ignores `X-Debug-Firebase-Uid` entirely unless AUTH_MODE is
 *      "emulated".
 *   3. The production ENV guard (SECURITY-REVIEW finding 5) refuses to boot with
 *      anything other than AUTH_MODE=firebase, so (2) can never be true in prod.
 *
 * The uid is read once per page load, deliberately: switching accounts is a
 * reload, which also throws away every screen's cached state. A live switcher
 * would leave one account's data on screen under another account's session.
 */

/**
 * Declared locally rather than relied on as an ambient global. This file is a
 * module, so this declaration is module-scoped and cannot collide with a global
 * one — and it means the file typechecks under any tsconfig, including the
 * narrower one `npm run verify:charts` uses, which reaches this module through
 * the api -> auth import chain and does not pull in Expo's globals.
 */
declare const __DEV__: boolean;

/**
 * Where a chosen uid is remembered between page loads, so it survives typing a
 * bare route into the address bar.
 *
 * The first version of this read the uid ONLY from the query string, which meant
 * `/feed` signed you out and bounced you to the sign-in screen while `/feed?uid=x`
 * worked — the guard behaving correctly, but indistinguishable from the app being
 * broken. Anyone reading a dashboard and then editing the path lost their session
 * for no visible reason.
 */
const STORAGE_KEY = "chirp.devAuthUid";

/** Passing this as the uid clears the impersonation and hands you back sign-in. */
const CLEAR_VALUES = new Set(["", "off", "none", "clear", "signout"]);

function readStored(): string | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored !== null && stored.trim().length > 0 ? stored.trim() : null;
  } catch {
    // localStorage throws in some privacy modes. A dev convenience is not worth
    // taking the whole app down over, so fall back to query-only behaviour.
    return null;
  }
}

function writeStored(uid: string | null): void {
  try {
    if (uid === null) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, uid);
  } catch {
    /* see readStored */
  }
}

/** The impersonated uid for this session, or null in any normal run. */
export function devAuthUid(): string | null {
  if (!__DEV__) return null;

  // Web: ?uid=dev-treasurer. One bookmarkable URL per account, which is what
  // makes the whole cast "readily available" — and the query string always wins,
  // so switching account is just editing the URL.
  if (typeof window !== "undefined" && typeof window.location?.search === "string") {
    const raw = new URLSearchParams(window.location.search).get("uid");
    if (raw !== null) {
      const fromQuery = raw.trim();
      if (CLEAR_VALUES.has(fromQuery.toLowerCase())) {
        writeStored(null);
        return null;
      }
      writeStored(fromQuery);
      return fromQuery;
    }
    // No uid in the URL: fall back to the last one chosen, so a bare route keeps
    // the session instead of silently dropping you at sign-in.
    const remembered = readStored();
    if (remembered !== null) return remembered;
  }

  // Native: there is no URL bar, so it comes from the environment instead.
  const fromEnv = process.env.EXPO_PUBLIC_DEV_UID;
  if (typeof fromEnv === "string" && fromEnv.trim().length > 0) return fromEnv.trim();

  return null;
}
