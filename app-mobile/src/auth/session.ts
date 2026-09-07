/** Firebase adapter: every token belongs to one UID and auth generation (c343). */
import {
  beforeAuthStateChanged,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  sendPasswordResetEmail,
  onIdTokenChanged as onFirebaseIdTokenChanged,
  signInWithEmailAndPassword,
  signOut,
  type User,
} from "firebase/auth";

import { Operation, OperationTimeoutError, REQUEST_TIMEOUT_MS } from "../api/operation";
import {
  currentIdentity, installToken, ownsIdentity, replaceIdentity, requireIdentity,
  SessionChangedError, tokenFor, type AuthIdentity,
} from "./identity";
import { getFirebaseAuth } from "./firebase";
import { devAuthUid } from "./devAuth";

const DEV_UID = devAuthUid();

let authIntent = 0;
export interface AuthAttempt {
  readonly id: number;
  readonly baselineUid: string | null;
  assertCurrent: () => void;
}
let transition: AuthAttempt | null = null;
let transitionOutcome: boolean | null = null;
let mutationQueue: Promise<unknown> = Promise.resolve();
let activeMutation: (() => void) | null = null;
let mutationGuardInstalled = false;
const pendingMutations = new Set<AuthAttempt>();
let tokenSequence = 0;
let pending: { owner: AuthIdentity; force: boolean; sequence: number; promise: Promise<string | null> } | null = null;

/** Read Firebase synchronously before capturing ownership; callbacks may arrive later. */
export function captureSession(): AuthIdentity {
  if (DEV_UID !== null) return replaceIdentity(DEV_UID);
  // A native cancellation may finish without entering the mutation queue while
  // an older SDK call is between its veto and currentUser commit. Do not release
  // quarantine until EVERY queued/in-flight SDK mutation has actually settled.
  if (transition && transitionOutcome !== null && pendingMutations.size === 0) {
    const actualUid = getFirebaseAuth().currentUser?.uid ?? null;
    if (transitionOutcome || actualUid === null || actualUid === transition.baselineUid) {
      transition = null;
      transitionOutcome = null;
    }
  }
  if (transition) return currentIdentity();
  return replaceIdentity(getFirebaseAuth().currentUser?.uid ?? null);
}

/** Refresh is shared only by callers in the same generation, with its own finite lifetime. */
export function getIdToken(forceRefresh = false, expected?: AuthIdentity): Promise<string | null> {
  const owner = captureSession();
  if (expected && !ownsIdentity(expected)) return Promise.reject(new SessionChangedError());
  const user = getFirebaseAuth().currentUser;
  if (transition || !user || user.uid !== owner.uid) return Promise.resolve(null);
  if (pending && ownsIdentity(pending.owner) && (!forceRefresh || pending.force)) return pending.promise;
  const sequence = ++tokenSequence;
  const operation = new Operation({}, owner);
  const promise = (async () => {
    try {
      const value = await operation.wait(Promise.resolve().then(() => {
        operation.assertCurrent();
        return user.getIdToken(forceRefresh);
      }));
      if (getFirebaseAuth().currentUser?.uid !== owner.uid || transition) throw new SessionChangedError();
      requireIdentity(owner);
      // A slower ordinary lookup cannot replace a newer forced refresh's token.
      if (sequence === tokenSequence) installToken(owner, value);
      return tokenFor(owner) ?? value;
    } finally {
      operation.dispose();
      if (pending?.sequence === sequence) pending = null;
    }
  })();
  pending = { owner, force: forceRefresh, sequence, promise };
  return promise;
}

/** Explicit auth actions invalidate ownership before any native/credential await. */
export function beginSignIn(): AuthAttempt {
  const id = ++authIntent;
  const attempt = {
    id, baselineUid: currentIdentity().uid,
    assertCurrent: () => { if (id !== authIntent) throw new SessionChangedError(); },
  };
  transition = attempt;
  transitionOutcome = null;
  replaceIdentity(null, true);
  return attempt;
}

/**
 * Send Firebase's own password-reset email (c385). Added with the auth restyle,
 * because that reference shot asks for a "Forgot password?" link and there was no
 * recovery path in the app at all — a sign-in screen whose only answer to a
 * forgotten password is "try again" is a dead end, not a design detail.
 *
 * Client-side only: Firebase sends and templates the mail, so there is no backend
 * route, no new secret and nothing to redeploy.
 *
 * DELIBERATELY DOES NOT REPORT WHETHER THE ADDRESS EXISTS. Firebase throws
 * auth/user-not-found here, and surfacing that to the UI would turn this box into
 * an account-enumeration oracle for any address someone cares to type. The caller
 * shows the same "check your inbox" either way — see the handler in sign-in.tsx,
 * which swallows exactly this code and no others.
 */
export async function sendPasswordReset(email: string): Promise<void> {
  await sendPasswordResetEmail(getFirebaseAuth(), email);
}


function finishTransition(attempt: AuthAttempt, success: boolean): void {
  if (transition !== attempt || attempt.id !== authIntent) return;
  // Record a completed/cancelled intent even when another mutation still owns
  // the SDK. captureSession reconciles it only after that owner settles, and
  // only if its eventual UID is permitted by this intent's original baseline.
  transitionOutcome = success;
  captureSession();
}

/**
 * Firebase commits currentUser BEFORE returning a credential. Serialize explicit
 * SDK mutations and veto stale commits at that public SDK boundary. Caller waits
 * are finite, but timing out does NOT release the mutex for an unabortable SDK
 * call: a stalled credential can delay another login until it settles. Token
 * refresh is deliberately outside this queue and remains generation scoped.
 */
export function runAuthMutation<T>(attempt: AuthAttempt, task: () => Promise<T>, allowLateLogout = false): Promise<T> {
  if (!mutationGuardInstalled) {
    beforeAuthStateChanged(getFirebaseAuth(), () => { activeMutation?.(); });
    mutationGuardInstalled = true;
  }
  pendingMutations.add(attempt);
  let expired = false;
  const validate = () => {
    attempt.assertCurrent();
    if (expired && !allowLateLogout) throw new OperationTimeoutError();
  };
  const underlying = mutationQueue.then(async () => {
    try {
      validate();
      activeMutation = validate;
      const result = await task();
      validate();
      finishTransition(attempt, true);
      return result;
    } catch (error) {
      finishTransition(attempt, false);
      throw error;
    } finally {
      pendingMutations.delete(attempt);
      if (activeMutation === validate) activeMutation = null;
      captureSession();
    }
  });
  // Only settlement of the underlying SDK mutation releases the queue.
  mutationQueue = underlying.then(() => {}, () => {});
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => { expired = true; reject(new OperationTimeoutError()); }, REQUEST_TIMEOUT_MS);
    underlying.then(value => { clearTimeout(timer); resolve(value); }, error => { clearTimeout(timer); reject(error); });
  });
}

/** Native cancellation/failure before an SDK mutation does not strand its owner. */
export function cancelSignIn(attempt: AuthAttempt): void {
  if (!pendingMutations.has(attempt)) finishTransition(attempt, false);
}

export async function prepareSignedInUser(user: User, attempt: AuthAttempt): Promise<void> {
  attempt.assertCurrent();
  if (getFirebaseAuth().currentUser?.uid !== user.uid) throw new SessionChangedError();
  const owner = captureSession();
  await getIdToken(false, owner);
  attempt.assertCurrent();
  requireIdentity(owner);
}

export async function signInWithEmail(email: string, password: string): Promise<User> {
  const attempt = beginSignIn();
  const credential = await runAuthMutation(attempt, () => signInWithEmailAndPassword(getFirebaseAuth(), email, password));
  await prepareSignedInUser(credential.user, attempt);
  return credential.user;
}

export async function signUpWithEmail(email: string, password: string): Promise<User> {
  const attempt = beginSignIn();
  const credential = await runAuthMutation(attempt, () => createUserWithEmailAndPassword(getFirebaseAuth(), email, password));
  await prepareSignedInUser(credential.user, attempt);
  return credential.user;
}

export async function signOutUser(): Promise<void> {
  const attempt = beginSignIn();
  // A requested logout may finish after its caller times out; it still cannot
  // clear a newer login, because the intent guard applies at SDK publication.
  await runAuthMutation(attempt, () => signOut(getFirebaseAuth()), true);
}

export function onAuthChanged(callback: (user: User | null) => void): () => void {
  return onAuthStateChanged(getFirebaseAuth(), user => {
    if ((user?.uid ?? null) !== (getFirebaseAuth().currentUser?.uid ?? null)) return;
    captureSession();
    callback(user);
  });
}

export function onIdTokenChanged(callback?: (user: User | null) => void): () => void {
  return onFirebaseIdTokenChanged(getFirebaseAuth(), user => {
    if ((user?.uid ?? null) !== (getFirebaseAuth().currentUser?.uid ?? null)) return;
    const owner = captureSession();
    if (!user) { callback?.(null); return; }
    void getIdToken(false, owner).then(() => {
      if (ownsIdentity(owner)) callback?.(user);
    }).catch(() => { /* Provider/request recovery owns token failures. */ });
  });
}
