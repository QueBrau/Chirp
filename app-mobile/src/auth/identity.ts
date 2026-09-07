/** Auth ownership shared by HTTP, token refresh, uploads and sockets (c343). */
export interface AuthIdentity {
  readonly uid: string | null;
  readonly generation: number;
  readonly signal: AbortSignal;
}

let controller = new AbortController();
let identity: AuthIdentity = { uid: null, generation: 0, signal: controller.signal };
let token: string | null = null;
const listeners = new Set<() => void>();

export class SessionChangedError extends Error {
  constructor() {
    super("The signed-in account changed. Please try again.");
    this.name = "SessionChangedError";
  }
}

export function currentIdentity(): AuthIdentity { return identity; }

export function ownsIdentity(owner: AuthIdentity): boolean {
  return owner.generation === identity.generation && owner.uid === identity.uid && !owner.signal.aborted;
}

export function requireIdentity(owner: AuthIdentity): void {
  if (!ownsIdentity(owner)) throw new SessionChangedError();
}

/** force is for explicit logout/sign-in, including logging back into the same UID. */
export function replaceIdentity(uid: string | null, force = false): AuthIdentity {
  if (!force && identity.uid === uid) return identity;
  const previous = controller;
  controller = new AbortController();
  token = null;
  identity = { uid, generation: identity.generation + 1, signal: controller.signal };
  previous.abort();
  for (const listener of listeners) listener();
  return identity;
}

/** The Firebase adapter supplies a captured owner, never an unscoped bearer. */
export function installToken(owner: AuthIdentity, value: string): void {
  requireIdentity(owner);
  token = value;
}

export function tokenFor(owner: AuthIdentity = identity): string | null {
  requireIdentity(owner);
  return token;
}

export function onIdentityChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
