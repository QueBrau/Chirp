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

export async function loadDeviceRecord(kv: KvPort, userId: string): Promise<DeviceRecord | null> {
  let raw: string | null;
  try {
    raw = await kv.get(deviceKey(userId));
  } catch {
    return null;
  }
  if (raw === null) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) return null;
    const { generation, deviceId, approved } = parsed;
    if (typeof generation !== "number" || !Number.isSafeInteger(generation) || generation < 1) return null;
    return {
      generation,
      deviceId: typeof deviceId === "string" && deviceId.length > 0 ? deviceId : null,
      approved: approved === true,
    };
  } catch {
    return null;
  }
}

export async function saveDeviceRecord(kv: KvPort, userId: string, record: DeviceRecord): Promise<void> {
  await kv.set(deviceKey(userId), JSON.stringify(record));
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
