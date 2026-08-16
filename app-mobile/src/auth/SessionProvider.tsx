/**
 * Session context: single source of truth for "who is signed in and how far
 * through onboarding are they" — fuses Firebase auth state with backend
 * bootstrap-completeness (GET /auth/me). Consumed by the (tabs) auth guard
 * and the (auth) screens instead of each re-deriving it from Firebase alone.
 *
 * Demo/mock mode (hasFirebaseConfig() false): status resolves to "ready"
 * immediately with no user — the pre-existing mock flow, unchanged.
 *
 * Real Firebase mode: subscribes to onAuthChanged. No Firebase user ->
 * "signedOut". A Firebase user -> fetchMe(): 200 -> "ready" with the backend
 * identity + memberships; 404 "user_not_registered" -> "unregistered".
 *
 * Staleness: every auth event and load bumps a generation counter, and a
 * load's result is discarded unless its generation is still current — a slow
 * response can never overwrite newer state (e.g. revive "ready" after the
 * user signed out mid-flight).
 *
 * Failures: a non-404 failure while a Firebase user exists retries (bounded)
 * instead of stranding the session; only when Firebase itself reports no user
 * (or the listener never fires and no user is present) do we conclude
 * "signedOut".
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { fetchMe, getCampus, type CampusOut, type UserOut } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { MembershipOut } from "@/api/chapters";

import { hasFirebaseConfig } from "./config";
import { getFirebaseAuth } from "./firebase";
import { onAuthChanged } from "./session";

export type SessionStatus = "loading" | "signedOut" | "unregistered" | "ready";

export interface SessionContextValue {
  status: SessionStatus;
  user: UserOut | null;
  memberships: MembershipOut[];
  /**
   * The signed-in user's campus (GET /campuses/{id}, resolved once here from
   * user.campus_id — c67: campus is a property of the session, not a
   * per-screen fetch. Every screen that used to call the hook independently
   * now reads the same value, so one Orgs mount no longer fires N identical
   * requests. Fails soft to null on a failed lookup for the same reason the
   * old per-screen hook did: campus name is a cosmetic label everywhere it's
   * used, so an absent eyebrow beats a wrong one or a crashed screen. Also
   * null while resolving — callers cannot and should not distinguish
   * "still fetching" from "failed"; both mean "do not render a name yet".
   */
  campus: CampusOut | null;
  /**
   * Re-run fetchMe() for the current Firebase user. Resolves true when the
   * session state was settled by a server answer (200 or 404), false when the
   * call failed and the previous status was kept — callers that navigate based
   * on session state should only proceed on true.
   */
  refresh: () => Promise<boolean>;
  /**
   * Seed the session directly from a successful POST /auth/bootstrap response:
   * flips to "ready" synchronously (no second round trip) and refreshes
   * memberships in the background.
   */
  applyBootstrap: (user: UserOut) => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

/** If onAuthChanged never fires AND Firebase knows no user, stop waiting. */
const LOADING_TIMEOUT_MS = 10_000;
/** Bounded retry for transient fetchMe failures while a Firebase user exists. */
const RETRY_DELAY_MS = 3_000;
const MAX_RETRIES = 3;

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>(hasFirebaseConfig() ? "loading" : "ready");
  const [user, setUser] = useState<UserOut | null>(null);
  const [memberships, setMemberships] = useState<MembershipOut[]>([]);
  const [campus, setCampus] = useState<CampusOut | null>(null);
  // Bumped on every auth event / seed / load start; a load only applies its
  // result while its generation is still current.
  const genRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearRetry = () => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  };

  const loadMe = useCallback(async (attempt = 0): Promise<boolean> => {
    const gen = ++genRef.current;
    try {
      const me = await fetchMe();
      if (genRef.current !== gen) return false; // superseded mid-flight
      setUser(me.user);
      setMemberships(me.memberships);
      setStatus("ready");
      return true;
    } catch (err) {
      if (genRef.current !== gen) return false;
      if (err instanceof ApiError && err.status === 404) {
        setUser(null);
        setMemberships([]);
        setStatus("unregistered");
        return true; // settled by a server answer
      }
      // Transient/network failure. If Firebase still has a user, retry rather
      // than stranding them; never assert "signedOut" while a session exists.
      const firebaseUser = getFirebaseAuth().currentUser;
      if (firebaseUser && attempt < MAX_RETRIES) {
        clearRetry();
        retryTimerRef.current = setTimeout(() => {
          if (genRef.current === gen) void loadMe(attempt + 1);
        }, RETRY_DELAY_MS);
        return false;
      }
      // Out of retries: only the never-left-"loading" case falls to signedOut
      // (the app must not hang); otherwise keep the prior status.
      setStatus((prev) => (prev === "loading" && !firebaseUser ? "signedOut" : prev));
      return false;
    }
  }, []);

  // Resolve campus ONCE per campus_id, here, instead of in every screen that
  // wants the name (c67). Keyed on campusId rather than re-running per mount:
  // any number of useCampus() readers below share this single fetch/result.
  const campusId = user?.campus_id ?? null;
  useEffect(() => {
    if (campusId === null) {
      setCampus(null);
      return;
    }

    // Guards a stale response landing after campusId has already moved on
    // (sign-out/sign-in as a different user mid-flight).
    let active = true;
    getCampus(campusId)
      .then((value) => {
        if (active) setCampus(value);
      })
      .catch(() => {
        if (active) setCampus(null);
      });

    return () => {
      active = false;
    };
  }, [campusId]);

  const refresh = useCallback(async (): Promise<boolean> => {
    if (!hasFirebaseConfig() || !getFirebaseAuth().currentUser) return false;
    return loadMe();
  }, [loadMe]);

  const applyBootstrap = useCallback(
    (bootstrapped: UserOut) => {
      genRef.current += 1; // discard any in-flight pre-bootstrap load
      clearRetry();
      setUser(bootstrapped);
      setMemberships([]);
      setStatus("ready");
      void loadMe(); // pick up memberships in the background
    },
    [loadMe],
  );

  useEffect(() => {
    if (!hasFirebaseConfig()) return;

    const timeout = setTimeout(() => {
      // Listener never fired: conclude signedOut only if Firebase agrees
      // there is no user; with a user present the loadMe retry path owns it.
      if (!getFirebaseAuth().currentUser) {
        setStatus((prev) => (prev === "loading" ? "signedOut" : prev));
      }
    }, LOADING_TIMEOUT_MS);

    const unsubscribe = onAuthChanged((firebaseUser) => {
      if (!firebaseUser) {
        genRef.current += 1; // invalidate in-flight loads from the old session
        clearRetry();
        setUser(null);
        setMemberships([]);
        setStatus("signedOut");
        return;
      }
      void loadMe();
    });

    return () => {
      clearTimeout(timeout);
      clearRetry();
      unsubscribe();
    };
  }, [loadMe]);

  const value = useMemo<SessionContextValue>(
    () => ({ status, user, memberships, campus, refresh, applyBootstrap }),
    [status, user, memberships, campus, refresh, applyBootstrap],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

/** Read the current session state. Must be called under SessionProvider (app/_layout.tsx). */
export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be called within a SessionProvider");
  return ctx;
}
