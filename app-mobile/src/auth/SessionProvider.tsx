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
 * identity + memberships; 404 "user_not_registered" -> "unregistered" (a
 * Firebase account that hasn't finished bootstrap() yet). Any other error
 * leaves the prior status alone — except it never strands the app on
 * "loading": a failed first call, or the listener simply never firing within
 * 10s, falls back to "signedOut" instead of hanging forever.
 */

import type { User } from "firebase/auth";
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

import { fetchMe, type UserOut } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { MembershipOut } from "@/api/chapters";

import { hasFirebaseConfig } from "./config";
import { onAuthChanged } from "./session";

export type SessionStatus = "loading" | "signedOut" | "unregistered" | "ready";

export interface SessionContextValue {
  status: SessionStatus;
  user: UserOut | null;
  memberships: MembershipOut[];
  /** Re-run fetchMe() for the current Firebase user. No-op if signed out. */
  refresh: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

/** Falls back to "signedOut" if onAuthChanged never fires — never hang on "loading". */
const LOADING_TIMEOUT_MS = 10_000;

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>(hasFirebaseConfig() ? "loading" : "ready");
  const [user, setUser] = useState<UserOut | null>(null);
  const [memberships, setMemberships] = useState<MembershipOut[]>([]);
  const firebaseUserRef = useRef<User | null>(null);
  const statusRef = useRef<SessionStatus>(status);
  statusRef.current = status;

  const loadMe = useCallback(async () => {
    try {
      const me = await fetchMe();
      setUser(me.user);
      setMemberships(me.memberships);
      setStatus("ready");
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUser(null);
        setMemberships([]);
        setStatus("unregistered");
        return;
      }
      // Unknown/network error: keep whatever status we had, but don't let a
      // failed first call strand the app on "loading" forever.
      if (statusRef.current === "loading") setStatus("signedOut");
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!firebaseUserRef.current) return;
    await loadMe();
  }, [loadMe]);

  useEffect(() => {
    if (!hasFirebaseConfig()) return;

    const timeout = setTimeout(() => {
      if (statusRef.current === "loading") setStatus("signedOut");
    }, LOADING_TIMEOUT_MS);

    const unsubscribe = onAuthChanged((firebaseUser) => {
      firebaseUserRef.current = firebaseUser;
      if (!firebaseUser) {
        setUser(null);
        setMemberships([]);
        setStatus("signedOut");
        return;
      }
      void loadMe();
    });

    return () => {
      clearTimeout(timeout);
      unsubscribe();
    };
  }, [loadMe]);

  const value = useMemo<SessionContextValue>(
    () => ({ status, user, memberships, refresh }),
    [status, user, memberships, refresh],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

/** Read the current session state. Must be called under SessionProvider (app/_layout.tsx). */
export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be called within a SessionProvider");
  return ctx;
}
