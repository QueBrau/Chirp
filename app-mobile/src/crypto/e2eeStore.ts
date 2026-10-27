/** What the app itself remembers about E2EE, apart from the native store (board c451).
 *
 * Two small records per signed-in user, both free of key material and of message text:
 *
 *  - the DEVICE RECORD (which generation and server device id this phone's device has) goes
 *    to secure storage (the Keychain), next to the store key it describes;
 *  - the APP STATE (the unsent-message outbox index and the "couldn't decrypt" placeholders)
 *    goes to ordinary app storage. The outbox holds ids only: a replay recovers the body
 *    from the encrypted native store, so plaintext is never written here.
 *
 * Both sit behind a three-method key-value port so the logic that uses them can be
 * executed with an in-memory fake (`npm run verify:e2ee-dm`).
 */

import type { AppState, DeviceRecord, OutboxEntry, UndecryptableRow } from "./e2eeTypes";
import { MAX_OUTBOX, MAX_REMEMBERED_FAILURES } from "./e2eeTypes";

export interface KvPort {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  remove(key: string): Promise<void>;
}

const deviceKey = (userId: string) => `chirp.e2ee.device.${userId}`;
const stateKey = (userId: string) => `chirp.e2ee.state.${userId}`;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// ---------------------------------------------------------------------------
// Device record
// ---------------------------------------------------------------------------

export async function loadDeviceRecord(kv: KvPort, userId: string, strict = false): Promise<DeviceRecord | null> {
  let raw: string | null;
  try {
    raw = await kv.get(deviceKey(userId));
  } catch (error) {
    if (strict) throw error;
    return null;
  }
  if (raw === null) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) throw new Error("device_record_invalid");
    const { generation, deviceId, approved, attempts } = parsed;
    if (typeof generation !== "number" || !Number.isSafeInteger(generation) || generation < 1) throw new Error("device_record_invalid");
    return {
      generation,
      deviceId: typeof deviceId === "string" && deviceId.length > 0 ? deviceId : null,
      approved: approved === true,
      ...(typeof attempts === "number" && Number.isSafeInteger(attempts) && attempts > 0 ? { attempts } : {}),
    };
  } catch (error) {
    if (strict) throw error;
    return null;
  }
}

export async function saveDeviceRecord(kv: KvPort, userId: string, record: DeviceRecord): Promise<void> {
  await kv.set(deviceKey(userId), JSON.stringify(record));
}

// ---------------------------------------------------------------------------
// Verified markers (c463)
// ---------------------------------------------------------------------------

/**
 * "I compared this safety number with them" (board c463), kept in secure storage under the
 * pair (this account, the contact). The VALUE is the 60-digit safety number that was marked,
 * which is itself a hash of both sides' approved device identities, so the marker is only
 * "verified" while the number now in force is the same one: when the contact (or this
 * account) adds, replaces or removes a phone the number changes and the marker is stale
 * without anything having to clear it. A number is public, so nothing secret is stored.
 */
const verifiedIndexKey = (userId: string) => `chirp.e2ee.verified-index.${userId}`;
const verifiedKey = (userId: string, peerUserId: string) => `chirp.e2ee.verified.${userId}.${peerUserId}`;

/** The safety number last marked verified for this pair, or null (none, or unreadable). */
export async function loadVerifiedNumber(kv: KvPort, userId: string, peerUserId: string): Promise<string | null> {
  try {
    const raw = await kv.get(verifiedKey(userId, peerUserId));
    return typeof raw === "string" && /^\d{60}$/.test(raw) ? raw : null;
  } catch {
    return null;
  }
}

export async function saveVerifiedNumber(kv: KvPort, userId: string, peerUserId: string, number: string): Promise<void> {
  // Index before the marker: interrupted writes can leave an extra index entry,
  // never a new untracked marker. Engine trust writes are serialized.
  const raw = await kv.get(verifiedIndexKey(userId));
  const peers: unknown = raw === null ? [] : JSON.parse(raw);
  if (!Array.isArray(peers) || peers.some(id => typeof id !== "string")) throw new Error("verified_index_invalid");
  await kv.set(verifiedIndexKey(userId), JSON.stringify([...new Set([...peers, peerUserId])]));
  await kv.set(verifiedKey(userId, peerUserId), number);
}

export async function clearVerifiedNumber(kv: KvPort, userId: string, peerUserId: string): Promise<void> {
  await kv.remove(verifiedKey(userId, peerUserId));
}

// ---------------------------------------------------------------------------
// App state: outbox index + placeholders
// ---------------------------------------------------------------------------

const EMPTY = (): AppState => ({ outbox: [], undecryptable: {} });

function parseOutbox(value: unknown): OutboxEntry[] {
  if (!Array.isArray(value)) return [];
  const out: OutboxEntry[] = [];
  for (const item of value) {
    if (!isRecord(item)) continue;
    const { clientMessageId, conversationId, peerUserId, createdAtMs, attempts, status, failure } = item;
    if (typeof clientMessageId !== "string" || typeof conversationId !== "string" || typeof peerUserId !== "string") continue;
    out.push({
      clientMessageId,
      conversationId,
      peerUserId,
      createdAtMs: typeof createdAtMs === "number" ? createdAtMs : 0,
      attempts: typeof attempts === "number" && attempts >= 0 ? attempts : 0,
      status: status === "failed" ? "failed" : "pending",
      failure: typeof failure === "string" ? failure : null,
    });
  }
  return out;
}

function parseUndecryptable(value: unknown): Record<string, UndecryptableRow[]> {
  if (!isRecord(value)) return {};
  const out: Record<string, UndecryptableRow[]> = {};
  for (const [conversationId, rows] of Object.entries(value)) {
    if (!Array.isArray(rows)) continue;
    const kept: UndecryptableRow[] = [];
    for (const row of rows) {
      if (!isRecord(row)) continue;
      const { messageId, createdAt, senderDeviceId, code, anchorSeq } = row;
      if (typeof messageId !== "string" || typeof createdAt !== "string") continue;
      kept.push({
        messageId,
        createdAt,
        senderDeviceId: typeof senderDeviceId === "string" ? senderDeviceId : "",
        code: typeof code === "string" ? code : "decrypt_failed",
        anchorSeq: typeof anchorSeq === "number" ? anchorSeq : 0,
      });
    }
    if (kept.length > 0) out[conversationId] = kept;
  }
  return out;
}

/**
 * The app state for one user. Loaded once, then mutated in place by `update`, which
 * persists a snapshot taken at the moment of the change; writes are serialized so a slow
 * one cannot land after a newer one. A write that fails makes `update` throw: the one place
 * that depends on it (recording an outbox intent BEFORE encrypting) must not go ahead
 * without it.
 */
export class AppStateStore {
  private state: Promise<AppState> | null = null;
  private chain: Promise<void> = Promise.resolve();

  constructor(private readonly kv: KvPort, private readonly userId: string) {}

  load(): Promise<AppState> {
    if (this.state === null) {
      this.state = (async () => {
        try {
          const raw = await this.kv.get(stateKey(this.userId));
          if (raw === null) return EMPTY();
          const parsed: unknown = JSON.parse(raw);
          if (!isRecord(parsed)) return EMPTY();
          return { outbox: parseOutbox(parsed.outbox), undecryptable: parseUndecryptable(parsed.undecryptable) };
        } catch {
          return EMPTY();
        }
      })();
    }
    return this.state;
  }

  async update<T>(change: (state: AppState) => T): Promise<T> {
    const state = await this.load();
    const result = change(state);
    for (const rows of Object.values(state.undecryptable)) {
      if (rows.length > MAX_REMEMBERED_FAILURES) rows.splice(0, rows.length - MAX_REMEMBERED_FAILURES);
    }
    const snapshot = JSON.stringify(state);
    const write = this.chain.then(() => this.kv.set(stateKey(this.userId), snapshot));
    this.chain = write.catch(() => undefined);
    await write;
    return result;
  }

  /** True when another unsent message can be accepted. */
  async hasRoom(): Promise<boolean> {
    return (await this.load()).outbox.length < MAX_OUTBOX;
  }
}

/** Delete only this account's app metadata. Old, pre-index public verification
 * markers cannot be enumerated by Expo SecureStore; known peers are also removed.
 * Native keys/history are wiped separately before this operation.
 */
export async function clearAccountMetadata(secure: KvPort, plain: KvPort, userId: string, knownPeers: string[] = []): Promise<void> {
  const raw = await secure.get(verifiedIndexKey(userId));
  const peers: unknown = raw === null ? [] : JSON.parse(raw);
  if (!Array.isArray(peers) || peers.some(id => typeof id !== "string")) throw new Error("verified_index_invalid");
  for (const peer of new Set([...peers, ...knownPeers])) await secure.remove(verifiedKey(userId, peer));
  await secure.remove(verifiedIndexKey(userId));
  await secure.remove(deviceKey(userId));
  await plain.remove(stateKey(userId));
}
