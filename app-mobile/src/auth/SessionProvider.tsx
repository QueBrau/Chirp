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
 * "signedOut". A Firebase user -> fetchMe(): 200 with suspended_at null ->
 * "ready" with the backend identity + memberships; 200 with suspended_at set
 * -> "suspended" (c129/c126) — still carries user/memberships, since the
 * suspended screen needs the timestamp to render; 404 "user_not_registered" ->
 * "unregistered".
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

import {
  fetchMe,
  getCampus,
  getCampusVerification,
  type CampusOut,
  type CampusVerificationStatus,
  type UserOut,
} from "@/api/auth";
import { ApiError, setAuthToken, setDebugFirebaseUid } from "@/api/client";
import type { MembershipOut } from "@/api/chapters";
import { chirpSocket } from "@/realtime/socket";

import { hasFirebaseConfig } from "./config";
import { devAuthUid } from "./devAuth";
import { getFirebaseAuth } from "./firebase";
import { onAuthChanged } from "./session";

// c129: "suspended" sits between "unregistered" and "ready" — a real user with a
// real backend row, just blocked. MeOut.suspended_at is what drives it, and
// per the manager's ruling on c126, GET /auth/me is the one route that stays
// reachable for a suspended caller specifically so this state is reachable at
// all — every other endpoint 403s them instead of returning a body to read.
export type SessionStatus = "loading" | "signedOut" | "unregistered" | "suspended" | "ready";

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
   * Whether the user currently holds a valid .edu verification (c110).
   *
   * REQUIRED FOR ANY CAMPUS-CONTENT DECISION. Since c88 the campus feed and Yak
   * are gated on a verification timestamp, NOT on having a campus_id — a user who
   * joined by chapter invite has a campus and is still refused. Screens that branch
   * on `user.campus_id !== null` will send that user into a 403.
   *
   * null means "not resolved yet" and is deliberately distinct from false: a screen
   * must not flash "verify your .edu" at an already-verified student during the
   * first frame. Wait for a boolean before deciding anything.
   */
  campusVerification: CampusVerificationStatus | null;
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
// Read once, at module load. Null in every normal run and in every release build
// (see ./devAuth). When set, Firebase is bypassed entirely and the session is
// whatever the seeded backend says that uid is.
const DEV_UID = devAuthUid();

const RETRY_DELAY_MS = 3_000;
const MAX_RETRIES = 3;

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>(
    DEV_UID !== null || hasFirebaseConfig() ? "loading" : "ready",
  );
  const [user, setUser] = useState<UserOut | null>(null);
  const [memberships, setMemberships] = useState<MembershipOut[]>([]);
  const [campus, setCampus] = useState<CampusOut | null>(null);
  const [campusVerification, setCampusVerification] = useState<CampusVerificationStatus | null>(
    null,
  );
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
      // c129: non-null here means suspended (see the SessionStatus comment) —
      // never re-derive this from a 403 elsewhere, since MeOut is the one place
      // that reliably reaches a body at all for a suspended caller.
      setStatus(me.user.suspended_at !== null ? "suspended" : "ready");
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
      const hasSession = DEV_UID !== null || getFirebaseAuth().currentUser !== null;
      if (hasSession && attempt < MAX_RETRIES) {
        clearRetry();
        retryTimerRef.current = setTimeout(() => {
          if (genRef.current === gen) void loadMe(attempt + 1);
        }, RETRY_DELAY_MS);
        return false;
      }
      // Out of retries: only the never-left-"loading" case falls to signedOut
      // (the app must not hang); otherwise keep the prior status.
      setStatus((prev) => (prev === "loading" && !hasSession ? "signedOut" : prev));
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

  // Resolve .edu verification once per session, alongside campus and for the same
  // reason (c67's pattern): every screen that gates campus content needs it, and
  // N screens must not each fire their own request.
  //
  // Keyed on the user id rather than campus_id: verification is a property of the
  // PERSON, and a user with campus_id null still needs a real answer — they are
  // exactly who the verify screen is for. Keying on campusId would skip the fetch
  // for the users who most need it.
  const userId = user?.id ?? null;
  useEffect(() => {
    if (userId === null) {
      setCampusVerification(null);
      return;
    }

    let active = true;
    getCampusVerification()
      .then((value) => {
        if (active) setCampusVerification(value);
      })
      .catch(() => {
        // Fails CLOSED, unlike campus above. A failed campus lookup costs a cosmetic
        // label; a failed verification lookup must not be read as "verified", so this
        // stays null and callers keep waiting rather than opening a gated surface.
        if (active) setCampusVerification(null);
      });

    return () => {
      active = false;
    };
  }, [userId]);

  // c63: the realtime gateway (c21) has been live and tested server-side since
  // before this session existed, and nothing in the app ever called
  // chirpSocket.connect() — found while building c129's WS suspension close
  // (4403 handling was correct and completely unreachable). One effect keyed
  // on `status` owns the whole lifecycle rather than scattering connect/
  // disconnect calls across every place status changes: "ready" is the only
  // status where a live connection makes sense — "suspended" already means
  // the gateway would refuse a new connection with 4403 anyway (c126), and
  // every other status means there is no authenticated session to hold one
  // open for. wsAuthProtocol() (src/api/client.ts) reads whatever token
  // setAuthToken last set, so this only needs to fire after that token is
  // real — which "ready" already guarantees, since loadMe's success path is
  // the same place that sets it.
  useEffect(() => {
    // c162 REMOVED a `if (DEV_UID !== null) return` guard here. Its reasoning was
    // that wsAuthProtocol() "reads whatever setAuthToken last set, which is null
    // here", so an impersonated session could only ever fail to connect. That is
    // not what that function does — it is `authToken ?? debugFirebaseUid`, so it
    // returns the dev uid, and ws/gateway.py's _resolve_uid accepts exactly that
    // as the offered subprotocol while AUTH_MODE is "emulated".
    //
    // Measured before changing it, rather than reasoned about twice: a browser
    // WebSocket opened to /ws with ["dev-secretary"] as its subprotocol reached
    // readyState OPEN, Redis reported PUBSUB NUMSUB user:<id> = 1, and a vote cast
    // by a different member arrived on that socket as a poll event. So the guard
    // was not protecting anyone from a failing connection — it was the reason
    // realtime was unreachable for all twelve seeded accounts (c159), for messages
    // as much as for polls.
    //
    // Safe in production regardless: DEV_UID is null in any release build, behind
    // the three independent locks in auth/devAuth.ts.
    if (status === "ready") {
      chirpSocket.connect();
    } else {
      chirpSocket.disconnect();
    }
  }, [status]);

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

  // Impersonated session (dev only). Sets the debug header and asks the backend
  // who that is; Firebase is never consulted.
  useEffect(() => {
    if (DEV_UID === null) return;
    setDebugFirebaseUid(DEV_UID);
    setAuthToken(null); // no bearer token exists for an emulated identity
    // Say WHO you are in the tab title. The uid now persists across page loads,
    // so without this there is no way to tell which of twelve accounts you are
    // looking at — and a sticky session you cannot see is its own confusion.
    // Title only: a banner would cover the very screens this exists to show.
    if (typeof document !== "undefined") {
      document.title = `${DEV_UID} · Chirp (dev)`;
    }
    void loadMe();
  }, [loadMe]);

  useEffect(() => {
    // Stand down when an impersonated uid owns the session, or the listener would
    // immediately overwrite it with "signedOut" (Firebase has no user here).
    if (DEV_UID !== null) return;
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
        setAuthToken(null);
        return;
      }
      // c95: own the bearer token before firing our own request. This listener
      // runs the moment Firebase resolves a user — on a cold start that is
      // before anything has called setAuthToken, so fetchMe() used to go out
      // with no Authorization header and come back 401 on EVERY sign-in and
      // every reload. A 401 is not a 404, so loadMe treated it as transient and
      // spent one of its three retries on a failure we caused ourselves,
      // shrinking the budget that exists for real network trouble.
      // signInWithEmail and the root layout's onIdTokenChanged both also set the
      // token; this is not redundant with them, it is the only one ordered
      // before the fetch that needs it.
      void (async () => {
        try {
          setAuthToken(await firebaseUser.getIdToken());
        } catch {
          // Token fetch failed: fall through to loadMe anyway rather than
          // stranding the session. If a token was already set it stays valid,
          // and loadMe's retry path owns the recovery either way.
        }
        await loadMe();
      })();
    });

    return () => {
      clearTimeout(timeout);
      clearRetry();
      unsubscribe();
    };
  }, [loadMe]);

  const value = useMemo<SessionContextValue>(
    () => ({ status, user, memberships, campus, campusVerification, refresh, applyBootstrap }),
    [status, user, memberships, campus, campusVerification, refresh, applyBootstrap],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

/** Read the current session state. Must be called under SessionProvider (app/_layout.tsx). */
export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be called within a SessionProvider");
  return ctx;
}
