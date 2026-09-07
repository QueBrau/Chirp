/** Backend session state belongs to the same UID/generation as its bearer (c343).
 * Transient failures expose retry after a finite budget; they never log Firebase out.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchMe, getCampus, getCampusVerification, type CampusOut, type CampusVerificationStatus, type UserOut } from "@/api/auth";
import { ApiError, setDebugFirebaseUid } from "@/api/client";
import { Operation } from "@/api/operation";
import type { MembershipOut } from "@/api/chapters";
import { chirpSocket, type SocketStatus } from "@/realtime/socket";
import { hasFirebaseConfig } from "./config";
import { devAuthUid } from "./devAuth";
import { getFirebaseAuth } from "./firebase";
import { captureSession, getIdToken, onAuthChanged } from "./session";
import { currentIdentity, onIdentityChanged, ownsIdentity, replaceIdentity, type AuthIdentity } from "./identity";

export type SessionStatus = "loading" | "recoverable" | "signedOut" | "unregistered" | "suspended" | "ready";

export interface SessionContextValue {
  status: SessionStatus;
  realtimeStatus: SocketStatus;
  realtimeRetrying: boolean;
  retryRealtime: () => Promise<void>;
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
   * REQUIRED FOR ANY CAMPUS-CONTENT DECISION. Since c88 the campus feed and Chirp
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
   * call failed and recovery is available — callers that navigate based
   * on session state should only proceed on true.
   */
  refresh: () => Promise<boolean>;
  /**
   * Seed the session directly from a successful POST /auth/bootstrap response:
   * flips to "ready" synchronously (no second round trip) and refreshes
   * memberships in the background.
   */
  applyBootstrap: (user: UserOut) => void;
  /**
   * Publish a known-good CampusVerificationStatus into the session directly (c379),
   * the same shape as applyBootstrap above: a caller that just received an
   * authoritative answer from the server hands it straight to the session instead
   * of waiting for a fetch that will never come on its own.
   *
   * Why one is needed at all: campusVerification is resolved once per userId (see
   * the effect below), and redeeming a code does not change the user's id — so
   * nothing would ever re-run that effect after POST
   * /auth/campus-verification/redeem, and the session would keep serving the
   * pre-redeem answer until the app restarted and re-mounted with a fresh
   * userId-keyed fetch.
   */
  applyCampusVerification: (verification: CampusVerificationStatus) => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);
const LOADING_TIMEOUT_MS = 10_000;
const DEV_UID = devAuthUid();
interface LoadOptions {
  owner?: AuthIdentity;
  signal?: AbortSignal;
  forceToken?: boolean;
  recoverOnFailure?: boolean;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>(DEV_UID !== null || hasFirebaseConfig() ? "loading" : "ready");
  const [user, setUser] = useState<UserOut | null>(null);
  const [memberships, setMemberships] = useState<MembershipOut[]>([]);
  const [campus, setCampus] = useState<CampusOut | null>(null);
  const [campusVerification, setCampusVerification] = useState<CampusVerificationStatus | null>(null);
  const [sessionGeneration, setSessionGeneration] = useState(currentIdentity().generation);
  const [realtimeStatus, setRealtimeStatus] = useState<SocketStatus>(chirpSocket.getStatus());
  const [realtimeRetrying, setRealtimeRetrying] = useState(false);
  const realtimeRetryRef = useRef<Promise<void> | null>(null);
  const realtimeRetrySequence = useRef(0);
  const genRef = useRef(0);
  const verificationGenRef = useRef(0);
  const loadRef = useRef<Operation | null>(null);

  const loadMe = useCallback(async (options: LoadOptions = {}): Promise<SessionStatus | null> => {
    const owner = options.owner ?? (DEV_UID !== null ? replaceIdentity(DEV_UID) : captureSession());
    if (owner.uid === null || !ownsIdentity(owner) || options.signal?.aborted) return null;
    const gen = ++genRef.current;
    loadRef.current?.cancel();
    const operation = new Operation({ timeoutMs: LOADING_TIMEOUT_MS, signal: options.signal }, owner);
    loadRef.current = operation;
    setStatus(prev => prev === "ready" || prev === "suspended" ? prev : "loading");
    try {
      operation.assertCurrent();
      if (DEV_UID === null) await operation.wait(getIdToken(options.forceToken ?? false, owner));
      operation.assertCurrent();
      const me = await fetchMe({ operation });
      if (genRef.current !== gen || !ownsIdentity(owner)) return null;
      if (me.user.firebase_uid !== owner.uid) throw new Error("Account changed. Please try again.");
      setUser(me.user);
      setMemberships(me.memberships);
      const nextStatus = me.user.suspended_at !== null ? "suspended" : "ready";
      setStatus(nextStatus);
      return nextStatus;
    } catch (err) {
      if (genRef.current !== gen || !ownsIdentity(owner)) return null;
      if (err instanceof ApiError && err.status === 404 && err.detail === "user_not_registered") {
        setUser(null);
        setMemberships([]);
        setStatus("unregistered");
        return "unregistered";
      }
      // A Firebase session still exists. Keep an already-resolved account usable,
      // or expose a retry screen; a backend outage is never a sign-out decision.
      setStatus(prev => !options.recoverOnFailure && (prev === "ready" || prev === "suspended") ? prev : "recoverable");
      return null;
    } finally {
      operation.dispose();
      if (loadRef.current === operation) loadRef.current = null;
    }
  }, []);

  const campusId = user?.campus_id ?? null;
  useEffect(() => {
    setCampus(null);
    if (campusId === null) return;
    const owner = currentIdentity();
    let active = true;
    getCampus(campusId).then(value => {
      if (active && ownsIdentity(owner)) setCampus(value);
    }).catch(() => {
      if (active && ownsIdentity(owner)) setCampus(null);
    });
    return () => { active = false; };
  }, [campusId, sessionGeneration]);

  const userId = user?.id ?? null;
  useEffect(() => {
    const gen = ++verificationGenRef.current;
    const owner = currentIdentity();
    setCampusVerification(null);
    if (userId === null) return;
    getCampusVerification()
      .then(value => {
        if (verificationGenRef.current === gen && ownsIdentity(owner)) setCampusVerification(value);
      })
      .catch(() => {
        // A failed verification lookup fails closed (c379).
        if (verificationGenRef.current === gen && ownsIdentity(owner)) setCampusVerification(null);
      });
    return () => { verificationGenRef.current += 1; };
  }, [userId, sessionGeneration]);

  // c379: an authoritative redeem wins over an older GET, even for the same user.
  // The callback itself belongs to the render's identity, so a stale screen cannot
  // publish into a replacement session before React has run its effect cleanup.
  const renderOwner = currentIdentity();
  const applyCampusVerification = useCallback((verification: CampusVerificationStatus) => {
    if (!ownsIdentity(renderOwner)) return;
    verificationGenRef.current += 1;
    setCampusVerification(verification);
  }, [renderOwner]);

  // Register one session decision-maker. Socket status is separate from account
  // status, so successful revalidation does not reset the socket run's auth budget.
  useEffect(() => {
    const unsubscribeStatus = chirpSocket.onStatus(setRealtimeStatus);
    const unsubscribeAuth = chirpSocket.setAuthHandlers({
      revalidate: async (owner, signal) => await loadMe({ owner, signal, forceToken: true, recoverOnFailure: true }) === "ready",
      exhausted: owner => {
        if (!ownsIdentity(owner)) return;
        setStatus(prev => prev === "suspended" || prev === "unregistered" || prev === "signedOut" ? prev : "recoverable");
      },
    });
    setRealtimeStatus(chirpSocket.getStatus());
    return () => { unsubscribeStatus(); unsubscribeAuth(); };
  }, [loadMe]);

  const retryRealtime = useCallback((): Promise<void> => {
    if (realtimeRetryRef.current) return realtimeRetryRef.current;
    const owner = currentIdentity(), sequence = ++realtimeRetrySequence.current;
    setRealtimeRetrying(true);
    const retry = loadMe({ owner, forceToken: true, recoverOnFailure: true }).then(result => {
      if (result === "ready" && ownsIdentity(owner) && sequence === realtimeRetrySequence.current) chirpSocket.retry();
    }).finally(() => {
      if (sequence === realtimeRetrySequence.current) {
        realtimeRetryRef.current = null;
        setRealtimeRetrying(false);
      }
    });
    realtimeRetryRef.current = retry;
    return retry;
  }, [loadMe]);

  useEffect(() => {
    if (status === "ready") chirpSocket.connect();
    else chirpSocket.disconnect();
    return () => chirpSocket.disconnect();
  }, [status, sessionGeneration]);

  const refresh = useCallback(async (): Promise<boolean> => {
    if (DEV_UID === null && (!hasFirebaseConfig() || !getFirebaseAuth().currentUser)) return false;
    return await loadMe() !== null;
  }, [loadMe]);

  const applyBootstrap = useCallback((bootstrapped: UserOut) => {
    if (!ownsIdentity(renderOwner) || bootstrapped.firebase_uid !== renderOwner.uid) return;
    genRef.current += 1;
    loadRef.current?.cancel();
    setUser(bootstrapped);
    setMemberships([]);
    setStatus(bootstrapped.suspended_at !== null ? "suspended" : "ready");
    void loadMe();
  }, [loadMe, renderOwner]);

  useEffect(() => {
    if (DEV_UID === null && !hasFirebaseConfig()) return;
    let observedGeneration = -1;
    const reconcile = () => {
      const owner = DEV_UID !== null ? replaceIdentity(DEV_UID) : captureSession();
      if (owner.generation === observedGeneration) return;
      observedGeneration = owner.generation;
      realtimeRetrySequence.current += 1;
      realtimeRetryRef.current = null;
      setRealtimeRetrying(false);
      genRef.current += 1;
      verificationGenRef.current += 1;
      loadRef.current?.cancel();
      setSessionGeneration(owner.generation);
      setUser(null);
      setMemberships([]);
      setCampus(null);
      setCampusVerification(null);
      if (owner.uid === null) setStatus("signedOut");
      else { setStatus("loading"); void loadMe(); }
    };
    const unsubscribeIdentity = onIdentityChanged(reconcile);
    if (DEV_UID !== null) {
      setDebugFirebaseUid(DEV_UID);
      if (typeof document !== "undefined") document.title = `${DEV_UID} · Chirp (dev)`;
      reconcile();
    }
    const unsubscribeAuth = DEV_UID === null ? onAuthChanged(reconcile) : () => {};
    // Cover a restored user even when an SDK listener is stalled. No current user
    // still waits for Firebase's initial hydration callback before declaring logout.
    if (DEV_UID === null && getFirebaseAuth().currentUser) reconcile();
    const timeout = setTimeout(() => {
      if (observedGeneration !== -1) return; // Each observed user has its own load budget.
      setStatus(prev => prev === "loading"
        ? (DEV_UID !== null || getFirebaseAuth().currentUser ? "recoverable" : "signedOut") : prev);
    }, LOADING_TIMEOUT_MS);
    return () => {
      clearTimeout(timeout);
      unsubscribeAuth();
      unsubscribeIdentity();
      realtimeRetrySequence.current += 1;
      realtimeRetryRef.current = null;
      genRef.current += 1;
      verificationGenRef.current += 1;
      loadRef.current?.cancel();
    };
  }, [loadMe]);

  const value = useMemo<SessionContextValue>(() => ({
    status, user, memberships, campus, campusVerification, refresh, applyBootstrap, applyCampusVerification, realtimeStatus, realtimeRetrying, retryRealtime,
  }), [status, user, memberships, campus, campusVerification, refresh, applyBootstrap, applyCampusVerification, realtimeStatus, realtimeRetrying, retryRealtime]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be called within a SessionProvider");
  return ctx;
}
