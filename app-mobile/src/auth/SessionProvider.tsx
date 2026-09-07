/** Backend session state belongs to the same UID/generation as its bearer (c343).
 * Transient failures expose retry after a finite budget; they never log Firebase out.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchMe, getCampus, getCampusVerification, type CampusOut, type CampusVerificationStatus, type UserOut } from "@/api/auth";
import { ApiError, setDebugFirebaseUid } from "@/api/client";
import { Operation } from "@/api/operation";
import type { MembershipOut } from "@/api/chapters";
import { chirpSocket } from "@/realtime/socket";
import { hasFirebaseConfig } from "./config";
import { devAuthUid } from "./devAuth";
import { getFirebaseAuth } from "./firebase";
import { captureSession, getIdToken, onAuthChanged } from "./session";
import { currentIdentity, onIdentityChanged, ownsIdentity, replaceIdentity } from "./identity";

export type SessionStatus = "loading" | "recoverable" | "signedOut" | "unregistered" | "suspended" | "ready";

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

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>(DEV_UID !== null || hasFirebaseConfig() ? "loading" : "ready");
  const [user, setUser] = useState<UserOut | null>(null);
  const [memberships, setMemberships] = useState<MembershipOut[]>([]);
  const [campus, setCampus] = useState<CampusOut | null>(null);
  const [campusVerification, setCampusVerification] = useState<CampusVerificationStatus | null>(null);
  const [sessionGeneration, setSessionGeneration] = useState(currentIdentity().generation);
  const genRef = useRef(0);
  const verificationGenRef = useRef(0);
  const loadRef = useRef<Operation | null>(null);

  const loadMe = useCallback(async (): Promise<boolean> => {
    const owner = DEV_UID !== null ? replaceIdentity(DEV_UID) : captureSession();
    if (owner.uid === null) return false;
    const gen = ++genRef.current;
    loadRef.current?.cancel();
    const operation = new Operation({ timeoutMs: LOADING_TIMEOUT_MS }, owner);
    loadRef.current = operation;
    setStatus(prev => prev === "ready" || prev === "suspended" ? prev : "loading");
    try {
      if (DEV_UID === null) await operation.wait(getIdToken(false, owner));
      operation.assertCurrent();
      const me = await fetchMe({ operation });
      if (genRef.current !== gen || !ownsIdentity(owner)) return false;
      if (me.user.firebase_uid !== owner.uid) throw new Error("Account changed. Please try again.");
      setUser(me.user);
      setMemberships(me.memberships);
      setStatus(me.user.suspended_at !== null ? "suspended" : "ready");
      return true;
    } catch (err) {
      if (genRef.current !== gen || !ownsIdentity(owner)) return false;
      if (err instanceof ApiError && err.status === 404 && err.detail === "user_not_registered") {
        setUser(null);
        setMemberships([]);
        setStatus("unregistered");
        return true;
      }
      // A Firebase session still exists. Keep an already-resolved account usable,
      // or expose a retry screen; a backend outage is never a sign-out decision.
      setStatus(prev => prev === "ready" || prev === "suspended" ? prev : "recoverable");
      return false;
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

  useEffect(() => {
    if (status === "ready") chirpSocket.connect();
    else chirpSocket.disconnect();
    return () => chirpSocket.disconnect();
  }, [status, sessionGeneration]);

  const refresh = useCallback(async (): Promise<boolean> => {
    if (DEV_UID === null && (!hasFirebaseConfig() || !getFirebaseAuth().currentUser)) return false;
    return loadMe();
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
      genRef.current += 1;
      verificationGenRef.current += 1;
      loadRef.current?.cancel();
    };
  }, [loadMe]);

  const value = useMemo<SessionContextValue>(() => ({
    status, user, memberships, campus, campusVerification, refresh, applyBootstrap, applyCampusVerification,
  }), [status, user, memberships, campus, campusVerification, refresh, applyBootstrap, applyCampusVerification]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be called within a SessionProvider");
  return ctx;
}
