/** Durable, account-bound local cleanup after authoritative deletion completion (c452). */

import type { AuthIdentity } from "../auth/identity";
import type { DeletionCompletionStatus, DeletionCompletionToken } from "../api/dataRequests";
import type { KvPort } from "./e2eeStore";

const INDEX = "chirp.deletion.pending.v1";
const key = (id: string) => `chirp.deletion.receipt.${id}`;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_PENDING = 16;

export interface PendingDeletion {
  userId: string;
  firebaseUid: string;
  requestId: string;
  token: string;
  expiresAt: string;
}

export interface DeletionCompletionPorts {
  secure: KvPort;
  issue(requestId: string): Promise<DeletionCompletionToken>;
  check(token: string): Promise<DeletionCompletionStatus>;
  purge(userId: string): Promise<void>;
  currentIdentity(): AuthIdentity;
  ownsIdentity(owner: AuthIdentity): boolean;
  now(): number;
  /** Sign out only the deleted account, after local erasure succeeds. */
  afterPurge(record: PendingDeletion, owner: AuthIdentity): Promise<void>;
}

/** No token, expired proof, a failed request or an auth error can authorize erasure. */
export class DeletionCompletion {
  private writes: Promise<unknown> = Promise.resolve();
  private checking: Promise<void> | null = null;

  constructor(private readonly ports: DeletionCompletionPorts) {}

  private serial<T>(work: () => Promise<T>): Promise<T> {
    const next = this.writes.then(work);
    this.writes = next.catch(() => undefined);
    return next;
  }

  private async ids(): Promise<string[]> {
    const raw = await this.ports.secure.get(INDEX);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > MAX_PENDING || parsed.some(id => typeof id !== "string" || !UUID.test(id))) {
      throw new Error("Invalid pending deletion index.");
    }
    return [...new Set(parsed as string[])];
  }

  private async read(requestId: string): Promise<PendingDeletion | null> {
    const raw = await this.ports.secure.get(key(requestId));
    if (raw === null) return null;
    const value = JSON.parse(raw) as Partial<PendingDeletion> | null;
    if (value === null || typeof value !== "object" || value.requestId !== requestId ||
      typeof value.userId !== "string" || !UUID.test(value.userId) ||
      typeof value.firebaseUid !== "string" || !value.firebaseUid ||
      typeof value.token !== "string" || !value.token || value.token.length > 2048 ||
      typeof value.expiresAt !== "string" || !Number.isFinite(Date.parse(value.expiresAt))) {
      throw new Error("Invalid pending deletion receipt.");
    }
    return value as PendingDeletion;
  }

  /** Retire unusable receipt metadata only; this never erases account/message data. */
  private async usableIds(): Promise<string[]> {
    const ids = await this.ids();
    const keep: string[] = [];
    for (const id of ids) {
      const record = await this.read(id);
      if (record && Date.parse(record.expiresAt) > this.ports.now()) keep.push(id);
      else await this.ports.secure.remove(key(id));
    }
    if (keep.length !== ids.length) await this.ports.secure.set(INDEX, JSON.stringify(keep));
    return keep;
  }

  /** Called while the owner can still authenticate, both on intake and status refresh. */
  remember(userId: string, requestId: string, owner: AuthIdentity): Promise<void> {
    return this.serial(async () => {
      const { ports } = this;
      if (!UUID.test(userId) || !UUID.test(requestId) || !owner.uid || !ports.ownsIdentity(owner)) {
        throw new Error("Account changed.");
      }
      const ids = await this.usableIds();
      if (!ports.ownsIdentity(owner)) throw new Error("Account changed.");
      const previous = await this.read(requestId);
      if (previous && (previous.userId !== userId || previous.firebaseUid !== owner.uid)) {
        throw new Error("Deletion receipt owner mismatch.");
      }
      // Refresh during the final week while authentication is still available.
      if (previous && Date.parse(previous.expiresAt) > ports.now() + 7 * 86_400_000) return;
      if (!ids.includes(requestId) && ids.length >= MAX_PENDING) throw new Error("Too many pending deletion receipts.");
      if (!ports.ownsIdentity(owner)) throw new Error("Account changed.");
      const issued = await ports.issue(requestId);
      if (!ports.ownsIdentity(owner)) throw new Error("Account changed.");
      if (issued.user_id !== userId || issued.request_id !== requestId ||
        typeof issued.token !== "string" || !issued.token || issued.token.length > 2048 ||
        typeof issued.expires_at !== "string" || !Number.isFinite(Date.parse(issued.expires_at)) ||
        Date.parse(issued.expires_at) <= ports.now()) throw new Error("Invalid deletion receipt.");
      const record: PendingDeletion = {
        userId, requestId, firebaseUid: owner.uid, token: issued.token, expiresAt: issued.expires_at,
      };
      // The receipt is bound to the captured account even if a storage await crosses sign-out.
      // Write the index first: a crash leaves a harmless missing record, never a lost secret.
      if (!ids.includes(requestId)) await ports.secure.set(INDEX, JSON.stringify([...ids, requestId]));
      await ports.secure.set(key(requestId), JSON.stringify(record));
    });
  }

  /** Checks are single-flight and bounded by the persisted index. Safe while signed out. */
  checkPending(): Promise<void> {
    if (this.checking !== null) return this.checking;
    const pending = this.runChecks().finally(() => { if (this.checking === pending) this.checking = null; });
    this.checking = pending;
    return pending;
  }

  private async runChecks(): Promise<void> {
    const { ports } = this;
    const ids = await this.serial(() => this.ids());
    for (const id of ids) {
      try {
        const record = await this.read(id);
        if (!record || Date.parse(record.expiresAt) <= ports.now()) continue;
        const owner = ports.currentIdentity();
        if (owner.uid !== null && owner.uid !== record.firebaseUid) continue;
        const proof = await ports.check(record.token);
        if (!ports.ownsIdentity(owner) || proof.completed !== true ||
          proof.user_id !== record.userId || proof.request_id !== record.requestId) continue;
        await ports.purge(record.userId);
        await ports.afterPurge(record, owner);
        await this.serial(async () => {
          await ports.secure.remove(key(id));
          const remaining = (await this.ids()).filter(value => value !== id);
          await ports.secure.set(INDEX, JSON.stringify(remaining));
        });
      } catch {
        // Preserve the receipt for another foreground/reconnect. Never infer deletion.
      }
    }
  }
}
