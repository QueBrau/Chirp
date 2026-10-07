/**
 * Device memory of the last campus a fully signed-in session resolved (board c434).
 *
 * WHY IT EXISTS. On a cold start the app cannot know which campus it is for until
 * GET /auth/me returns, the session then resolves user.campus_id, and the campus
 * lookup lands. Until that happens there is nothing campus-colored to draw, which is
 * why the session-restore window used to be a blank screen. The loading screen
 * (components/LoadingScreen.tsx) paints the campus colors and name immediately by
 * rendering from THIS memory instead: remember the campus after a successful session,
 * read it back on the next launch, before the session has loaded.
 *
 * WHEN IT IS WRITTEN: only once the session is "ready" AND the campus has resolved.
 * A half-loaded session never overwrites a good memory with a worse one.
 *
 * WHY IT IS CLEARED ON SIGN-OUT. A signed-out launch is not a returning user, so there
 * is no school to flash at it; and on a shared phone, keeping the last person's school
 * would greet the next person with it. Signed out means forgotten.
 *
 * WHAT COLORS ARE STORED. Whatever useAppearance().campusColors holds, not a table of
 * its own. Today that is the UNCG mock for everyone (app/_layout.tsx passes
 * MOCK_CAMPUS_COLORS into AppearanceProvider, because campus colors are not server data
 * yet). When campus colors become real server data and the provider is fed them, this
 * starts storing the real ones with no change here.
 *
 * THE READ STARTS AT MODULE LOAD, not when a component first asks, so the value is
 * usually ready before the session finishes restoring and the loading screen can render
 * it on its very first frame. Everything read back is validated, and every storage
 * failure degrades to "no memory" (null): this is cosmetic, and never a reason to break
 * sign-in or to throw out of a render.
 */

import AsyncStorage from "@react-native-async-storage/async-storage";
import { useEffect, useState } from "react";

import { useAppearance } from "@/theme";

import { useSession } from "./SessionProvider";

const STORAGE_KEY = "chirp.lastCampus.v1";
const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

export interface LastCampus {
  name: string;
  primary: string;
  secondary: string;
}

/** Parses a stored value. Anything missing, corrupt or the wrong shape is null. Never throws. */
function parseLastCampus(raw: string | null): LastCampus | null {
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null) return null;
    const { name, primary, secondary } = value as Record<string, unknown>;
    if (typeof name !== "string" || name.length === 0) return null;
    if (typeof primary !== "string" || !HEX_COLOR.test(primary)) return null;
    if (typeof secondary !== "string" || !HEX_COLOR.test(secondary)) return null;
    return { name, primary, secondary };
  } catch {
    return null;
  }
}

// Module-level cache. `cacheSettled` is true once the memory is KNOWN, either because
// the initial read finished or because this session wrote or cleared it first. The
// second case is why the read below checks it: a slow getItem must not land on top of a
// newer write and resurrect the old campus.
let cache: LastCampus | null = null;
let cacheSettled = false;

const initialRead: Promise<void> = (async () => {
  try {
    const stored = parseLastCampus(await AsyncStorage.getItem(STORAGE_KEY));
    if (!cacheSettled) cache = stored;
  } catch {
    // getItem rejected: no memory, same as a first launch.
  }
  cacheSettled = true;
})();

/**
 * The remembered campus, plus whether the read has finished at all.
 *
 * `resolved` is separate from `campus === null` because "still reading" and "nothing
 * remembered" call for different screens: the first should draw nothing yet (the read
 * takes milliseconds, and flashing a neutral frame before swapping to campus colors
 * would be a visible flicker), the second should draw the neutral tile.
 */
export function useLastCampus(): { campus: LastCampus | null; resolved: boolean } {
  const [state, setState] = useState(() => ({ campus: cache, resolved: cacheSettled }));

  useEffect(() => {
    let active = true;
    // Also covers a read that finished between the first render and this effect.
    void initialRead.then(() => {
      if (!active) return;
      // Same value as the synchronous initializer already used: no extra render.
      setState(prev => (prev.resolved && prev.campus === cache ? prev : { campus: cache, resolved: true }));
    });
    return () => {
      active = false;
    };
  }, []);

  return state;
}

function remember(next: LastCampus): void {
  const unchanged =
    cache !== null &&
    cache.name === next.name &&
    cache.primary === next.primary &&
    cache.secondary === next.secondary;
  if (unchanged) return;
  cache = next;
  cacheSettled = true;
  AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(next)).catch(() => {});
}

function forget(): void {
  cache = null;
  cacheSettled = true;
  AsyncStorage.removeItem(STORAGE_KEY).catch(() => {});
}

/**
 * Keeps the device memory in step with the session. Mount once, inside both
 * SessionProvider and AppearanceProvider (app/_layout.tsx's RootLayoutNav does).
 *
 * "ready" with a resolved campus writes; "signedOut" clears; every other status
 * (loading, recoverable, unregistered, suspended) leaves the memory alone, so a
 * backend blip or a half-finished sign-up never wipes a returning user's campus.
 *
 * Any "signedOut" clears, on purpose: forgetting is the trade for the shared-phone rule
 * above. That includes SessionProvider's 10 s hydration timeout, which also declares
 * "signedOut" when Firebase has not produced a user yet, so a very slow hydration costs a
 * returning user a neutral loading screen on the next launch, nothing more.
 */
export function useRememberCampus(): void {
  const { status, campus } = useSession();
  const { campusColors } = useAppearance();
  const name = campus?.name ?? null;
  const { primary, secondary } = campusColors;

  useEffect(() => {
    if (status === "signedOut") {
      forget();
      return;
    }
    if (status !== "ready" || name === null) return;
    remember({ name, primary, secondary });
  }, [status, name, primary, secondary]);
}
