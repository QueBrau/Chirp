/** The encrypted-DM engine: device lifecycle, send, receive, outbox (board c451, M4).
 *
 * E2EE-DESIGN.md section 7 as code. It sits between the screens and two boundaries it does
 * not own: the native core (`chirpCrypto`, src/crypto/native.ts) and the v2 HTTP API
 * (src/api/e2ee.ts). Both arrive as PORTS, so `npm run verify:e2ee-dm` can execute this very
 * file against fakes: generation retry, mismatch-retry-once, outbox replay and hard-stop
 * gating are checked on the real logic, not on a copy of it.
 *
 * Ground rules, each enforced here rather than hoped for:
 *  - Plaintext lives in the encrypted native store only. The outbox index holds ids; a
 *    replay reads the body back from the store. Nothing here logs or persists a body.
 *  - The core encrypts or it throws. There is no path that posts anything but the legs the
 *    core returned (the only send call is `sendMessageV2`, which takes legs, not text).
 *  - A trust hard stop blocks SENDING with a typed error; receiving never checks trust.
 *  - Claiming a key burns a key on the server, so a key is claimed only for a recipient the
 *    core says has no session yet (`hasSession`).
 *  - Sign-out locks the store. Nothing here ever wipes it except the one case where the
 *    store holds a device the server never confirmed (an unusable half-registration).
 */

import type { chirpCrypto, DirectoryDevice, Leg, PlaintextMessage, SignedKey, TrustState } from "./native";
import type * as E2eeApi from "../api/e2ee";
import type { MessageHistoryV2Out } from "../api/e2ee";
import {
  apiCode,
  asApiError,
  isConflict,
  isDeviceUnusable,
  isAccountDeleting,
  isForbidden,
  isPermanentDecryptFailure,
  isServerWithoutV2,
  isRetryLater,
  nativeCode,
  safeCode,
  staleGenerationMax,
} from "./e2eeErrors";
import { legBody, registrationBody, toDirectoryDevice, toSignedKey, uploadBody } from "./e2eeMapping";
import {
  AppStateStore,
  clearVerifiedNumber,
  loadDeviceRecord,
  loadVerifiedNumber,
  saveDeviceRecord,
  saveVerifiedNumber,
  type KvPort,
} from "./e2eeStore";
import { directoryFingerprint, pairFingerprint } from "./e2eeTrust";
import {
  E2EE_ENVELOPE_VERSION,
  E2EE_SUITE,
  E2eeSendError,
  E2eeTrustError,
  HISTORY_PAGE,
  INACTIVE_STATE,
  INITIAL_ONE_TIME_KEYS,
  KEY_LOW_WATER,
  KEY_TARGET,
  ACCOUNT_DELETING,
  DEVICE_REVOKED,
  MAX_DM_BODY_CHARS,
  MAX_RESET_REVOKE_PASSES,
  MAX_UNCONFIRMED_REGISTRATIONS,
  REGISTRATION_UNCONFIRMED,
  MAX_SYNC_BACKLOG,
  SERVER_NOT_READY,
  type AppState,
  type DmTrust,
  type E2eeState,
  type OutboxEntry,
  type PendingDevice,
  type SafetyView,
  type SendOutcome,
  type TrustBlock,
  type UndecryptableRow,
} from "./e2eeTypes";

// ---------------------------------------------------------------------------
// Ports
// ---------------------------------------------------------------------------

export type CryptoPort = Pick<
  typeof chirpCrypto,
  | "unlock"
  | "lock"
  | "wipe"
  | "createDevice"
  | "markPublished"
  | "topUpKeys"
  | "pendingKeys"
  | "publicIdentity"
  | "observeDirectory"
  | "acceptIdentityChange"
  | "approveDevice"
  | "safetyNumber"
  | "deviceComparisonCode"
  | "hasSession"
  | "encrypt"
  | "decrypt"
  | "listMessages"
>;

export type ApiPort = Pick<
  typeof E2eeApi,
  | "registerDeviceV2"
  | "uploadKeysV2"
  | "getKeyCountV2"
  | "getUserDirectoryV2"
  | "listPendingDevicesV2"
  | "approveDeviceV2"
  | "revokeDeviceV2"
  | "claimKeysV2"
  | "sendMessageV2"
  | "listMessagesV2"
>;

export interface ConversationsPort {
  /** Who the other member of a conversation is. `peerUserId` is null for a group. */
  getPeer(conversationId: string, myUserId: string): Promise<{ kind: "dm" | "group"; peerUserId: string | null }>;
}

export interface EnginePorts {
  crypto: CryptoPort;
  api: ApiPort;
  /** Holds the device record (Keychain). */
  secure: KvPort;
  /** Holds the outbox index and placeholders. */
  plain: KvPort;
  /** Whether this build has the native module at all. */
  available: () => boolean;
  /** A fresh lowercase UUID v4. */
  newId: () => string;
  now: () => number;
  conversations: ConversationsPort;
  /**
   * Optional diagnostics for failures the engine absorbs (a key top-up that did not go
   * through, a replay that could not reach the server). Receives a place name and a FIXED
   * code (a native error code, an API `detail`, or "error"): never a message, a body or an id.
   */
  report?: (where: string, code: string) => void;
}

type Handle = Awaited<ReturnType<CryptoPort["unlock"]>>;

/** What is true while a device is up: one per started engine, dropped by `stop()`. */
interface Session {
  userId: string;
  generation: number;
  deviceId: string;
  handle: Handle;
  run: number;
}

interface DirectorySet {
  me: DirectoryDevice;
  /** My other approved devices. */
  mine: DirectoryDevice[];
  /** All of the peer's approved devices. */
  peer: DirectoryDevice[];
}

/**
 * `state`      this phone's device state changed.
 * `thread`     one conversation's rows changed.
 * `approvals`  the list of new phones waiting for THIS phone's approval changed (c463).
 * `trust`      a trust decision changed for one user (verified, accepted); headers re-read it.
 */
export type EngineEvent =
  | { type: "state" }
  | { type: "thread"; conversationId: string }
  | { type: "approvals" }
  | { type: "trust"; userId: string };

export interface SyncResult {
  received: number;
  undecryptable: number;
}

export interface ThreadSnapshot {
  /** Newest first, as the core returns them. */
  messages: PlaintextMessage[];
  /** True when older local messages exist beyond this page. */
  hasOlder: boolean;
  outbox: OutboxEntry[];
  undecryptable: UndecryptableRow[];
}

/** How long a fetched directory is reused before a send fetches it again. */
const DIRECTORY_TTL_MS = 30_000;
/** Local messages read per page. The core's own maximum is 200. */
const LOCAL_PAGE = 100;

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

export class E2eeEngine {
  private state: E2eeState = INACTIVE_STATE;
  private readonly listeners = new Set<(event: EngineEvent) => void>();

  private session: Session | null = null;
  private startedFor: string | null = null;
  private starting: Promise<E2eeState> | null = null;
  /** Settles when the latest bring-up has finished unwinding (success, failure or abort). */
  private unwinding: Promise<void> = Promise.resolve();
  private run = 0;
  /** Foreground re-checks of a server without v2 used in this session (at most 1). */
  private notReadyRechecks = 0;
  private store: AppStateStore | null = null;
  private readonly directories = new Map<string, { at: number; devices: DirectoryDevice[] }>();
  /** New phones of this account waiting for THIS phone to approve them (c463). */
  private pending: PendingDevice[] = [];
  /** Pending phones the person chose to decide on later; cleared when the app returns to the foreground. */
  private readonly snoozed = new Set<string>();
  /** The one Reset security run in flight, so a second tap joins it instead of starting another. */
  private resetting: Promise<void> | null = null;

  private sendTail: Promise<unknown> = Promise.resolve();
  private replaying: Promise<void> | null = null;
  private maintaining: Promise<void> | null = null;
  private readonly syncing = new Map<string, { promise: Promise<SyncResult>; again: boolean }>();

  constructor(private readonly ports: EnginePorts) {}

  // ---- observation ---------------------------------------------------------------

  getState(): E2eeState {
    return this.state;
  }

  subscribe(listener: (event: EngineEvent) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(event: EngineEvent): void {
    for (const listener of [...this.listeners]) {
      try {
        listener(event);
      } catch {
        // A broken subscriber must not break the engine.
      }
    }
  }

  private setState(next: Partial<E2eeState> & Pick<E2eeState, "status">): E2eeState {
    // `undefined` keeps the current value; an explicit null clears it.
    this.state = {
      status: next.status,
      userId: next.userId === undefined ? this.state.userId : next.userId,
      deviceId: next.deviceId === undefined ? this.state.deviceId : next.deviceId,
      generation: next.generation === undefined ? this.state.generation : next.generation,
      reason: next.reason ?? null,
    };
    // Only a ready phone can approve another one: a prompt for a new phone must not outlive
    // this phone being removed from the account, put back to waiting, or stopped.
    if (this.state.status !== "ready") this.setPending([]);
    this.emit({ type: "state" });
    return this.state;
  }

  private alive(run: number): boolean {
    return this.run === run;
  }

  /** Tell the diagnostics port about a failure that is being absorbed. Codes only. */
  private report(where: string, error: unknown): void {
    try {
      // Only a fixed, word-shaped code ever leaves here: never a message, body or id.
      this.ports.report?.(safeCode(where), safeCode(typeof error === "string" ? error : (nativeCode(error) ?? apiCode(error) ?? "error")));
    } catch {
      // Diagnostics never break the engine.
    }
  }

  // ---- lifecycle -----------------------------------------------------------------

  /**
   * Bring this phone's device up for `userId`: unlock the store, register if there is no
   * device, check approval, stock keys. Safe to call repeatedly: a second call for the
   * same user returns the state (or the call already in flight); a call for another user
   * stops the first. Never throws; the outcome is `getState()`.
   *
   * A server that has no v2 API yet ends in `unavailable` / `server_not_ready` WITHOUT
   * creating or wiping anything: the probe runs before the store is touched. Calling
   * `start` again does not probe again (the answer stands for the session); see `recheck`.
   */
  start(userId: string, options: { recheck?: boolean } = {}): Promise<E2eeState> {
    if (this.starting !== null && this.startedFor === userId) return this.starting;
    if (this.startedFor === userId && this.state.status !== "error") return Promise.resolve(this.state);
    if (this.startedFor !== null && this.startedFor !== userId) void this.stop();
    // A start that is not itself a re-check begins a new session's budget.
    if (options.recheck !== true) this.notReadyRechecks = 0;
    const run = ++this.run;
    this.startedFor = userId;
    // A new bring-up never overlaps an older one still unwinding: two would race for the
    // same store ("store_in_use") and one handle would leak.
    const earlier = this.unwinding;
    const quiet = options.recheck === true;
    const promise = earlier
      .then(() => this.bringUp(userId, run, quiet))
      .catch((error: unknown) => this.failStart(run, error));
    this.starting = promise;
    this.unwinding = promise.then(() => undefined);
    void promise.finally(() => {
      if (this.starting === promise) this.starting = null;
    });
    return promise;
  }

  /** Sign-out or account switch: lock the store and forget the session. Wipes nothing. */
  async stop(): Promise<void> {
    this.run += 1;
    this.startedFor = null;
    this.starting = null;
    const session = this.session;
    this.session = null;
    this.store = null;
    this.directories.clear();
    this.syncing.clear();
    this.snoozed.clear();
    if (this.pending.length > 0) {
      this.pending = [];
      this.emit({ type: "approvals" });
    }
    this.setState({ ...INACTIVE_STATE });
    const locking = session ? this.lockQuietly(session.handle) : Promise.resolve();
    this.unwinding = Promise.all([this.unwinding, locking]).then(() => undefined);
    await locking;
  }

  /**
   * Ask again where this phone stands: a pending phone may have been approved; a failed
   * start is tried again. Cheap enough to call when the app returns to the foreground.
   *
   * `source` matters for exactly one state: a server without v2 (`server_not_ready`). That
   * answer is looked at again only when the app returns to the FOREGROUND, and only once per
   * session; a socket reconnect or a manual retry never re-probes it, so there is no loop.
   */
  async recheck(source: "foreground" | "reconnect" | "manual" = "manual"): Promise<E2eeState> {
    const { status, userId } = this.state;
    if (userId === null) return this.state;
    if (status === "unavailable" && this.state.reason === SERVER_NOT_READY) {
      if (source !== "foreground" || this.notReadyRechecks >= 1) return this.state;
      this.notReadyRechecks += 1;
      this.startedFor = null; // Nothing was opened, so there is nothing to stop first.
      return this.start(userId, { recheck: true });
    }
    // A revoked device is revoked for good (c463: Reset security and "Not me" are how it happens).
    // Starting again would find its stored record, call it approved, and say "ready" until the
    // next call failed; the only way back is a fresh install, which registers a new device.
    if (status === "error" && this.state.reason === DEVICE_REVOKED) return this.state;
    if (status === "error") {
      await this.stop();
      return this.start(userId);
    }
    const session = this.session;
    if (status !== "pending_approval" || session === null) return this.state;
    try {
      if (await this.isListed(session.userId, session.deviceId)) {
        await saveDeviceRecord(this.ports.secure, session.userId, {
          generation: session.generation,
          deviceId: session.deviceId,
          approved: true,
        });
        if (this.alive(session.run)) {
          this.setState({ status: "ready" });
          void this.afterReady();
        }
      }
    } catch (error) {
      // Still pending, or the server could not be asked; stay as we are.
      this.report("recheck", error);
    }
    return this.state;
  }

  private async bringUp(userId: string, run: number, quiet: boolean): Promise<E2eeState> {
    // A quiet start is the one re-check of a server that had no v2: the screen keeps saying
    // "coming soon" while it asks, instead of flashing "getting ready".
    if (!quiet) this.setState({ status: "starting", userId, deviceId: null, generation: null, reason: null });
    if (!this.ports.available()) {
      return this.setState({ status: "unavailable" });
    }
    const { crypto, secure } = this.ports;

    const record = await loadDeviceRecord(secure, userId);
    // One install registers ONE device. A registration whose answer was lost can leave a
    // pending device this phone cannot see, and a retry would add another: after a few
    // unconfirmed attempts stop and say so, instead of piling up orphans on the server.
    if (record !== null && record.deviceId === null && (record.attempts ?? 0) >= MAX_UNCONFIRMED_REGISTRATIONS) {
      throw new StartFailure(REGISTRATION_UNCONFIRMED);
    }
    // A phone that has never been confirmed by the server asks one question BEFORE it makes
    // or opens anything: does this server have the v2 API? (Reading your own directory is
    // allowed and claims nothing.) Production is not redeployed with /v2 yet, so the next
    // build must not create a device, register, or wipe against a server that cannot answer.
    // A phone that is already registered skips this: it has history to show and the server
    // that registered it did have v2.
    if (record === null || record.deviceId === null) await this.probeServer(userId);
    let generation = record?.generation ?? 1;
    let handle = await crypto.unlock(userId, generation);
    try {
      if (!this.alive(run)) throw new Superseded();
      let deviceId = record?.deviceId ?? null;
      let approved = record?.approved ?? false;

      const identity = await crypto.publicIdentity(handle);
      if (identity === null) {
        if (record !== null) {
          // A device from an earlier install (the Keychain outlives the app's files) or an
          // abandoned attempt used this generation. It is not ours to reuse, and the
          // server may still list it: take the next generation and register fresh.
          await crypto.wipe(userId, generation);
          generation = record.generation + 1;
          handle = await crypto.unlock(userId, generation);
          deviceId = null;
          approved = false;
        }
      } else if (deviceId === null) {
        // The store holds a device the server never confirmed to us (a crash or a lost
        // reply between registering and remembering the answer).
        const found = await this.findOwnDevice(userId, identity.ed25519);
        if (found !== null) {
          deviceId = found;
          approved = true;
          await saveDeviceRecord(secure, userId, { generation, deviceId, approved });
          const stuck = await crypto.pendingKeys(handle);
          await crypto.markPublished(handle, deviceId, stuck.map((key) => key.keyId));
        } else {
          // A half-registered device has no recoverable binding signature, so it cannot be
          // registered now. It holds no history (it never got a device id): start over.
          await crypto.wipe(userId, generation);
          handle = await crypto.unlock(userId, generation);
        }
      }

      if (deviceId === null) {
        const registered = await this.register(userId, run, generation, handle, record?.deviceId === null ? (record.attempts ?? 0) : 0);
        handle = registered.handle;
        generation = registered.generation;
        deviceId = registered.deviceId;
        approved = registered.approved;
      }
      if (!this.alive(run)) throw new Superseded();

      if (!approved) {
        try {
          approved = await this.isListed(userId, deviceId);
        } catch (error) {
          if (!isRetryLater(error)) throw error;
          // Cannot ask the server and never confirmed approval: do not guess.
          throw new StartFailure("network");
        }
        if (approved) await saveDeviceRecord(secure, userId, { generation, deviceId, approved: true });
      }

      const session: Session = { userId, generation, deviceId, handle, run };
      this.session = session;
      this.store = new AppStateStore(this.ports.plain, userId);
      if (approved) {
        const ready = this.setState({ status: "ready", userId, deviceId, generation });
        void this.afterReady();
        return ready;
      }
      return this.setState({ status: "pending_approval", userId, deviceId, generation });
    } catch (error) {
      if (this.session?.handle !== handle) await this.lockQuietly(handle);
      throw error;
    }
  }

  private failStart(run: number, error: unknown): E2eeState {
    if (error instanceof Superseded || !this.alive(run)) return this.state;
    // The server has no v2 API: not an error, and no diagnostics line either (expected).
    if (isServerWithoutV2(error)) return this.setState({ status: "unavailable", reason: SERVER_NOT_READY });
    // The account is being deleted: not an error and not retryable. No diagnostics, no retry.
    if (isAccountDeleting(error)) return this.setState({ status: "unavailable", reason: ACCOUNT_DELETING });
    // No answer, or a server asking for patience, is "network" wherever in the start it happened.
    const reason =
      error instanceof StartFailure ? error.reason : (nativeCode(error) ?? (isRetryLater(error) ? "network" : (apiCode(error) ?? "start_failed")));
    this.report("start", reason);
    return this.setState({ status: "error", reason });
  }

  /** Register this phone's device. The first attempt may use an already-unlocked store. */
  private async register(
    userId: string,
    run: number,
    startGeneration: number,
    firstHandle: Handle,
    priorAttempts: number,
  ) {
    const { crypto, api, secure } = this.ports;
    let generation = startGeneration;
    let handle = firstHandle;
    let sent = priorAttempts; // Registration requests sent without ever being told a device id.
    try {
      for (let attempt = 0; ; attempt++) {
        if (sent >= MAX_UNCONFIRMED_REGISTRATIONS) throw new StartFailure(REGISTRATION_UNCONFIRMED);
        const created = await crypto.createDevice(handle, INITIAL_ONE_TIME_KEYS);
        // Before the request: from here a crash leaves a store with a device and no id, which
        // the next start knows how to clean up. The count is saved BEFORE the request too, so a
        // crash mid-request is still counted.
        sent += 1;
        await saveDeviceRecord(secure, userId, { generation, deviceId: null, approved: false, attempts: sent });
        let out: Awaited<ReturnType<ApiPort["registerDeviceV2"]>>;
        try {
          out = await api.registerDeviceV2(registrationBody(created, generation));
        } catch (error) {
          const max = staleGenerationMax(error);
          if (max === null || attempt >= 1) throw error;
          // This generation is taken (a reinstall, or another phone). The store we just
          // made was never registered, so drop it and take the next free generation.
          await crypto.wipe(userId, generation);
          generation = Math.max(max, generation) + 1;
          await saveDeviceRecord(secure, userId, { generation, deviceId: null, approved: false, attempts: sent });
          if (!this.alive(run)) throw new Superseded();
          handle = await crypto.unlock(userId, generation);
          if (!this.alive(run)) {
            await this.lockQuietly(handle);
            throw new Superseded();
          }
          continue;
        }
        // The answer is remembered before anything else can fail.
        await saveDeviceRecord(secure, userId, { generation, deviceId: out.id, approved: out.approved });
        const keyIds = [...created.oneTimeKeys.map((key) => key.keyId), created.fallbackKey.keyId];
        await crypto.markPublished(handle, out.id, keyIds);
        return { handle, generation, deviceId: out.id, approved: out.approved };
      }
    } catch (error) {
      // Whatever stopped this, the store it has open must not stay open: the next start (the
      // person's retry, or Reset security's repair) unlocks the same namespace.
      await this.lockQuietly(handle);
      throw error;
    }
  }

  /**
   * Is there a v2 API behind this server? Reads this user's own directory. 200 (even with no
   * devices) means yes; 404/405 rethrows the original error so `failStart` can say
   * `server_not_ready`; no answer or a busy server is a plain start failure (`network`) like
   * any other, and any other answer is surfaced by its fixed code.
   */
  private async probeServer(userId: string): Promise<void> {
    try {
      await this.ports.api.getUserDirectoryV2(userId);
    } catch (error) {
      if (isServerWithoutV2(error)) throw error;
      if (isRetryLater(error)) throw new StartFailure("network");
      throw error;
    }
  }

  /** Whether the server's directory for `userId` lists `deviceId` (so: approved, unrevoked). */
  private async isListed(userId: string, deviceId: string): Promise<boolean> {
    const out = await this.ports.api.getUserDirectoryV2(userId);
    return out.devices.some((device) => device.device_id === deviceId);
  }

  private async findOwnDevice(userId: string, ed25519: string): Promise<string | null> {
    try {
      const out = await this.ports.api.getUserDirectoryV2(userId);
      return out.devices.find((device) => device.identity_ed25519_b64 === ed25519)?.device_id ?? null;
    } catch (error) {
      if (isRetryLater(error)) throw new StartFailure("network");
      throw error;
    }
  }

  private async lockQuietly(handle: Handle): Promise<void> {
    try {
      await this.ports.crypto.lock(handle);
    } catch {
      // Locking is idempotent and best-effort on the way out.
    }
  }

  /** Once the device is usable: stock the server with keys, then finish unsent messages. */
  private async afterReady(): Promise<void> {
    try {
      await this.maintainKeys();
    } catch (error) {
      // Stocking keys is retried on the next start and after the next prekey message.
      this.report("maintainKeys", error);
    }
    try {
      await this.replayOutbox();
    } catch (error) {
      // Same: the outbox stays put until a replay gets through.
      this.report("replayOutbox", error);
    }
  }

  // ---- keys ----------------------------------------------------------------------

  /**
   * Keep the server stocked with one-time keys: re-upload anything generated but never
   * confirmed (a crash between generating and uploading), then top up below the low-water
   * mark. Coalesced: a second call while one runs joins it.
   */
  maintainKeys(): Promise<void> {
    if (this.maintaining !== null) return this.maintaining;
    const session = this.session;
    if (session === null || this.state.status !== "ready") return Promise.resolve();
    const promise = this.doMaintainKeys(session).finally(() => {
      if (this.maintaining === promise) this.maintaining = null;
    });
    this.maintaining = promise;
    return promise;
  }

  private async doMaintainKeys(session: Session): Promise<void> {
    const { crypto, api } = this.ports;
    const pending = await crypto.pendingKeys(session.handle);
    if (pending.length > 0) await this.publishKeys(session, pending);

    const count = await api.getKeyCountV2(session.deviceId);
    if (count.one_time_keys_available >= KEY_LOW_WATER) return;
    const wanted = KEY_TARGET - count.one_time_keys_available;
    let fresh: SignedKey[];
    try {
      fresh = await crypto.topUpKeys(session.handle, wanted);
    } catch (error) {
      // The local pool is already full of keys the server has not handed out. Nothing to add.
      if (nativeCode(error) === "too_many_keys") return;
      throw error;
    }
    await this.publishKeys(session, fresh);
  }

  /**
   * Upload signed keys and tell the core they are published. If the server says some key
   * id is already there, an earlier upload of this batch reached it and only the local
   * confirmation was lost: the batch is treated as published. If that guess is ever wrong
   * the cost is a few keys the server never has, which the count-based top-up above
   * covers; it cannot lose a message, because the fallback key always answers a claim.
   */
  private async publishKeys(session: Session, keys: SignedKey[]): Promise<void> {
    const { crypto, api } = this.ports;
    try {
      await api.uploadKeysV2(session.deviceId, uploadBody(keys));
    } catch (error) {
      if (!isConflict(error, "key_id_reused")) throw error;
    }
    await crypto.markPublished(session.handle, session.deviceId, keys.map((key) => key.keyId));
  }

  // ---- directories and trust -----------------------------------------------------

  /**
   * One user's approved devices. `force` skips the cache; `strict` (c463) also refuses to fall
   * back to a stale copy when the server cannot be asked. Sending may use the last directory it
   * saw (the server's mismatch check corrects it); a person comparing safety numbers or
   * accepting a changed code must be looking at the directory as it is NOW.
   */
  private async directory(userId: string, force: boolean, strict = false): Promise<DirectoryDevice[]> {
    const cached = this.directories.get(userId);
    if (!force && cached && this.ports.now() - cached.at < DIRECTORY_TTL_MS) return cached.devices;
    try {
      const out = await this.ports.api.getUserDirectoryV2(userId);
      const devices = out.devices.map((row) => toDirectoryDevice(userId, row));
      this.directories.set(userId, { at: this.ports.now(), devices });
      return devices;
    } catch (error) {
      // Offline: the last directory we saw is still the best we have. A mismatch on the
      // server will correct it if it has changed.
      if (!strict && isRetryLater(error) && cached) return cached.devices;
      throw error;
    }
  }

  /** Both directories, observed for trust. Throws `E2eeSendError` for anything that blocks. */
  private async prepare(session: Session, peerUserId: string, force: boolean): Promise<DirectorySet> {
    let [mineAll, peer] = await Promise.all([
      this.directory(session.userId, force),
      this.directory(peerUserId, force),
    ]);
    let me = mineAll.find((device) => device.deviceId === session.deviceId);
    if (me === undefined && !force) {
      // A cached list can predate this phone's approval; ask once before deciding.
      mineAll = await this.directory(session.userId, true);
      me = mineAll.find((device) => device.deviceId === session.deviceId);
    }
    if (me === undefined) throw new E2eeSendError("device_unusable");
    if (peer.length === 0) throw new E2eeSendError("peer_no_devices");
    const block = await this.observeTrust(session, [
      { userId: peerUserId, devices: peer },
      { userId: session.userId, devices: mineAll },
    ]);
    if (block !== null) throw new E2eeSendError("blocked", block);
    return { me, mine: mineAll.filter((device) => device.deviceId !== me.deviceId), peer };
  }

  /** Every group observed for trust (a user with no devices is skipped), by user id. */
  private async observeStates(
    session: Session,
    groups: { userId: string; devices: DirectoryDevice[] }[],
  ): Promise<Map<string, TrustState>> {
    const states = new Map<string, TrustState>();
    for (const group of groups) {
      if (group.devices.length === 0) continue;
      states.set(group.userId, await this.ports.crypto.observeDirectory(session.handle, group.userId, group.devices));
    }
    return states;
  }

  /** The first group (in the order given) that is not trusted, else null. */
  private async observeTrust(
    session: Session,
    groups: { userId: string; devices: DirectoryDevice[] }[],
  ): Promise<TrustBlock | null> {
    const states = await this.observeStates(session, groups);
    for (const group of groups) {
      const state = states.get(group.userId);
      if (state !== undefined && state !== "trusted") return { userId: group.userId, reason: state };
    }
    return null;
  }

  /**
   * Whether the conversation with `peerUserId` is clear to send to, for the banner, and
   * whether its safety number is marked verified. Looks at the directories (cached if the
   * network is down). Never throws: when nothing can be known, nothing is claimed.
   */
  async trustFor(peerUserId: string): Promise<DmTrust> {
    const session = this.readySession();
    if (session === null) return { block: null, verified: false, fingerprint: null };
    try {
      const [mine, peer] = await Promise.all([this.directory(session.userId, false), this.directory(peerUserId, false)]);
      const block = await this.observeTrust(session, [{ userId: peerUserId, devices: peer }, { userId: session.userId, devices: mine }]);
      if (block !== null) {
        // What was seen when the banner went up, so an accept can refuse if it moves again.
        const seen = block.userId === session.userId ? mine : peer;
        return { block, verified: false, fingerprint: directoryFingerprint(seen) };
      }
      return { block, verified: await this.isMarkedVerified(session, peerUserId, mine, peer), fingerprint: null };
    } catch (error) {
      this.report("trustFor", error);
      return { block: null, verified: false, fingerprint: null };
    }
  }

  /** Is the safety number of these two directories the one the person marked verified? Never throws. */
  private async isMarkedVerified(
    session: Session,
    peerUserId: string,
    mine: DirectoryDevice[],
    peer: DirectoryDevice[],
  ): Promise<boolean> {
    try {
      const marked = await loadVerifiedNumber(this.ports.secure, session.userId, peerUserId);
      if (marked === null || mine.length === 0 || peer.length === 0) return false;
      return (await this.safetyNumberOf(session, peerUserId, mine, peer)) === marked;
    } catch {
      return false;
    }
  }

  private safetyNumberOf(session: Session, peerUserId: string, mine: DirectoryDevice[], peer: DirectoryDevice[]): Promise<string> {
    return this.ports.crypto.safetyNumber(
      session.userId,
      mine.map((device) => device.identityEd25519),
      peerUserId,
      peer.map((device) => device.identityEd25519),
    );
  }

  // ---- trust UX (c463) -----------------------------------------------------------
  //
  // Everything a person decides about trust goes through here: looking at a safety number,
  // marking it verified, accepting a changed code, approving or refusing a new phone, and
  // Reset security. Each decision is made against directories fetched NOW (never a stale
  // copy) and, where the person looked at something first, bound to exactly what they saw.

  private requireReady(): Session {
    const session = this.readySession();
    if (session === null) throw new E2eeTrustError("not_ready");
    return session;
  }

  /** A failure of a trust call as the fixed reason a screen can word. */
  private trustFailure(error: unknown): E2eeTrustError {
    if (error instanceof E2eeTrustError) return error;
    if (isAccountDeleting(error)) return new E2eeTrustError("account_deletion");
    // A native refusal is an answer, not a missing network: `isRetryLater` treats every
    // non-HTTP error as "no answer", so the native code is looked at first.
    if (nativeCode(error) !== null) return new E2eeTrustError("rejected");
    if (isRetryLater(error)) return new E2eeTrustError("network");
    return new E2eeTrustError("rejected");
  }

  /** Both directories, fetched fresh and strictly (no stale fallback), for a person to look at. */
  private async freshPair(session: Session, peerUserId: string) {
    const [mine, peer] = await Promise.all([
      this.directory(session.userId, true, true),
      this.directory(peerUserId, true, true),
    ]);
    if (!mine.some((device) => device.deviceId === session.deviceId)) throw new E2eeTrustError("not_ready");
    if (peer.length === 0) throw new E2eeTrustError("peer_no_devices");
    return { mine, peer };
  }

  /**
   * The safety number with `peerUserId`, from directories read right now, and where each
   * side stands against this phone's pins. The reading also records that standing in the
   * core, so what the screen shows and what a send would check cannot disagree.
   */
  async safetyView(peerUserId: string): Promise<SafetyView> {
    try {
      const session = this.requireReady();
      const { mine, peer } = await this.freshPair(session, peerUserId);
      const states = await this.observeStates(session, [
        { userId: peerUserId, devices: peer },
        { userId: session.userId, devices: mine },
      ]);
      const number = await this.safetyNumberOf(session, peerUserId, mine, peer);
      const state = states.get(peerUserId) ?? "trusted";
      const ownState = states.get(session.userId) ?? "trusted";
      const marked = await loadVerifiedNumber(this.ports.secure, session.userId, peerUserId);
      return {
        peerUserId,
        number,
        state,
        ownState,
        verified: state === "trusted" && ownState === "trusted" && marked === number,
        fingerprint: pairFingerprint(peer, mine),
        peerFingerprint: directoryFingerprint(peer),
        ownFingerprint: directoryFingerprint(mine),
      };
    } catch (error) {
      throw this.trustFailure(error);
    }
  }

  /**
   * Mark (or unmark) the safety number the person is looking at as verified. `fingerprint` is
   * the view's: if either directory moved since, nothing is marked and the person looks again.
   * A number cannot be marked while a hard stop is open: accept it first, then verify what
   * was accepted. Returns whether the pair is verified now.
   */
  async setVerified(peerUserId: string, verified: boolean, fingerprint: string): Promise<boolean> {
    try {
      const view = await this.safetyView(peerUserId);
      if (view.fingerprint !== fingerprint) throw new E2eeTrustError("changed_again");
      const { secure } = this.ports;
      const me = this.requireReady().userId;
      if (!verified) {
        await clearVerifiedNumber(secure, me, peerUserId);
        this.emit({ type: "trust", userId: peerUserId });
        return false;
      }
      if (view.state !== "trusted" || view.ownState !== "trusted") throw new E2eeTrustError("blocked");
      await saveVerifiedNumber(secure, me, peerUserId, view.number);
      this.emit({ type: "trust", userId: peerUserId });
      return true;
    } catch (error) {
      throw this.trustFailure(error);
    }
  }

  /**
   * The person accepted the changed code of `userId` (the contact, or this account's own
   * phones). The core pins exactly the directory it observed last, so this reads that user's
   * directory fresh, refuses if it is not the one the person was shown (`expected`, from the
   * banner or the safety screen; null accepts whatever is current), has the core observe it,
   * accepts, and asks the core again. Returns the state after: `trusted` when it took.
   * Verified marks are untouched: a new directory means a new number, so the old mark is
   * simply stale.
   */
  async acceptIdentityChange(userId: string, expected: string | null): Promise<TrustState> {
    try {
      const session = this.requireReady();
      const devices = await this.directory(userId, true, true);
      if (devices.length === 0) throw new E2eeTrustError("peer_no_devices");
      if (expected !== null && directoryFingerprint(devices) !== expected) throw new E2eeTrustError("changed_again");
      const { crypto } = this.ports;
      const before = await crypto.observeDirectory(session.handle, userId, devices);
      if (before !== "trusted") {
        try {
          await crypto.acceptIdentityChange(session.handle, userId);
        } catch (error) {
          // The directory went back to what is pinned between the two calls: nothing to accept.
          if (nativeCode(error) !== "nothing_to_accept") throw error;
        }
      }
      const after = await crypto.observeDirectory(session.handle, userId, devices);
      this.emit({ type: "trust", userId });
      return after;
    } catch (error) {
      throw this.trustFailure(error);
    }
  }

  // ---- new phone: approving from an existing phone -------------------------------

  /** New phones waiting for this phone's approval that the person has not put off. */
  getPendingApprovals(): PendingDevice[] {
    return this.pending.filter((device) => !this.snoozed.has(device.deviceId));
  }

  /**
   * Ask the server which of this account's phones are waiting for approval, and keep them with
   * their comparison codes. Cheap: the app calls it on start, on returning to the foreground
   * and every half minute while in use. A failure keeps the list as it was. Only an approved
   * (ready) phone can approve, so any other state clears the list.
   */
  async refreshPendingDevices(options: { foreground?: boolean } = {}): Promise<PendingDevice[]> {
    if (options.foreground === true) this.snoozed.clear();
    const session = this.readySession();
    if (session === null) {
      this.setPending([]);
      return [];
    }
    let rows: Awaited<ReturnType<ApiPort["listPendingDevicesV2"]>>["devices"];
    try {
      rows = (await this.ports.api.listPendingDevicesV2()).devices;
    } catch (error) {
      this.report("pending_devices", error);
      return this.getPendingApprovals();
    }
    const next: PendingDevice[] = [];
    try {
      for (const row of rows) {
        // This phone is approved, so it cannot be in its own pending list; a row that claims
        // to be, or one of another suite, is not something this phone can approve.
        if (row.id === session.deviceId || row.suite !== E2EE_SUITE) continue;
        next.push({
          deviceId: row.id,
          generation: row.generation,
          label: row.device_label,
          createdAt: row.created_at,
          identityCurve25519: row.identity_curve25519_b64,
          identityEd25519: row.identity_ed25519_b64,
          bindingSignature: row.binding_signature_b64,
          code: await this.ports.crypto.deviceComparisonCode(row.identity_ed25519_b64),
        });
      }
    } catch (error) {
      this.report("pending_devices", error);
      return this.getPendingApprovals();
    }
    if (!this.alive(session.run)) return [];
    this.setPending(next);
    return this.getPendingApprovals();
  }

  private setPending(next: PendingDevice[]): void {
    const same =
      next.length === this.pending.length && next.every((device, i) => device.deviceId === this.pending[i].deviceId);
    this.pending = next;
    for (const id of [...this.snoozed]) if (!next.some((device) => device.deviceId === id)) this.snoozed.delete(id);
    if (!same) this.emit({ type: "approvals" });
  }

  /** Decide on this phone later: the prompt goes away until the app returns to the foreground. */
  snoozePending(deviceId: string): void {
    this.snoozed.add(deviceId);
    this.emit({ type: "approvals" });
  }

  /**
   * "Approve": this phone signs the new phone's binding exactly as listed (the core checks the
   * new phone's own signature and that it belongs to this account), the signature goes to the
   * server, and the list is read again. A retry of an approval that already landed is success.
   */
  async approvePending(deviceId: string): Promise<void> {
    try {
      const session = this.requireReady();
      const target = this.pending.find((device) => device.deviceId === deviceId);
      if (target === undefined) throw new E2eeTrustError("gone");
      const approval = await this.ports.crypto.approveDevice(session.handle, {
        userId: session.userId,
        generation: target.generation,
        identityCurve25519: target.identityCurve25519,
        identityEd25519: target.identityEd25519,
        bindingSignature: target.bindingSignature,
      });
      try {
        await this.ports.api.approveDeviceV2(deviceId, {
          approver_device_id: session.deviceId,
          approval_signature_b64: approval.signature,
        });
      } catch (error) {
        // The new phone was revoked meanwhile (or is not a phone of this account): it is gone.
        if (isForbidden(error, "device_revoked")) throw new E2eeTrustError("gone");
        // This phone is not an approved one as far as the server is concerned: it cannot sign.
        if (isForbidden(error, "invalid_approver")) throw new E2eeTrustError("rejected");
        // Approved already (by this phone, or another): the person's intent is satisfied.
        if (!isConflict(error, "device_already_approved")) throw error;
      }
      // This account's own directory just grew; the next send must not use the old one.
      this.directories.delete(session.userId);
      this.emit({ type: "trust", userId: session.userId });
      await this.refreshPendingDevices();
    } catch (error) {
      throw this.trustFailure(error);
    }
  }

  /** "Not me": revoke the pending phone so it can never be approved. Already gone counts as done. */
  async denyPending(deviceId: string): Promise<void> {
    try {
      const session = this.requireReady();
      try {
        await this.ports.api.revokeDeviceV2(deviceId);
      } catch (error) {
        // Not found or not ours means it is not a pending phone of this account any more.
        const http = asApiError(error);
        if (http === null || (http.status !== 404 && http.status !== 403)) throw error;
      }
      this.directories.delete(session.userId);
      await this.refreshPendingDevices();
    } catch (error) {
      throw this.trustFailure(error);
    }
  }

  /**
   * This phone's own comparison code, for the "waiting for approval" banner: the same digits
   * the approving phone shows for it. Null before a device exists.
   */
  async ownComparisonCode(): Promise<string | null> {
    const session = this.session;
    if (session === null) return null;
    try {
      const identity = await this.ports.crypto.publicIdentity(session.handle);
      if (identity === null) return null;
      return await this.ports.crypto.deviceComparisonCode(identity.ed25519);
    } catch (error) {
      this.report("own_code", error);
      return null;
    }
  }

  // ---- Reset security ------------------------------------------------------------

  /**
   * For a phone that is waiting for approval and cannot get it (the other phone is lost, or
   * this is a reinstall and the server still lists the old install as approved): revoke every
   * OTHER phone of this account, then register this phone again as the account's root. The
   * server makes a registration a root exactly when the account has no approved, unrevoked
   * device left, so the order matters and is the point: others first, then register.
   *
   * What it guarantees, each checked by `verify:e2ee-trust`:
   *  - the current phone is never revoked; its own pending registration is revoked only AFTER
   *    the new root exists, because by then nothing can use it (its store was wiped);
   *  - the revoke pass runs at most MAX_RESET_REVOKE_PASSES times, and a phone that appears
   *    meanwhile makes it stop with `reset_incomplete` rather than go round again;
   *  - exactly one new device is registered (plus the core's single stale-generation retry),
   *    and if the server does not make it the root the run ends as `reset_incomplete`
   *    without trying again: nothing here loops, and another attempt is the person's tap;
   *  - nothing is wiped locally until every other phone is revoked, so a network failure
   *    leaves this phone exactly as it was, and a failure after that leaves a state the next
   *    start repairs by registering (which is then the root).
   * A second call while one is running joins it.
   */
  resetSecurity(): Promise<void> {
    if (this.resetting !== null) return this.resetting;
    const session = this.session;
    if (this.state.status !== "pending_approval" || session === null) {
      return Promise.reject(new E2eeTrustError("not_pending"));
    }
    const promise = this.doReset(session)
      .catch((error: unknown) => {
        throw this.trustFailure(error);
      })
      .finally(() => {
        if (this.resetting === promise) this.resetting = null;
      });
    this.resetting = promise;
    return promise;
  }

  /** Every device of this account other than the current one: approved ones and pending ones. */
  private async otherDeviceIds(session: Session): Promise<string[]> {
    const { api } = this.ports;
    const [directory, pending] = await Promise.all([
      api.getUserDirectoryV2(session.userId),
      api.listPendingDevicesV2(),
    ]);
    const ids = new Set<string>();
    for (const device of directory.devices) ids.add(device.device_id);
    for (const device of pending.devices) ids.add(device.id);
    ids.delete(session.deviceId);
    return [...ids];
  }

  private async doReset(session: Session): Promise<void> {
    const { crypto, api, secure } = this.ports;
    const { userId } = session;

    // 1. Revoke every other phone. Server side only, idempotent, nothing local changes yet.
    for (let pass = 0; ; pass++) {
      const others = await this.otherDeviceIds(session);
      if (others.length === 0) break;
      if (pass >= MAX_RESET_REVOKE_PASSES) throw new E2eeTrustError("reset_incomplete");
      for (const id of others) {
        if (id === session.deviceId) continue; // Never the current phone.
        try {
          await api.revokeDeviceV2(id);
        } catch (error) {
          // Not found or not ours: it is not a device of this account any more.
          const http = asApiError(error);
          if (http === null || (http.status !== 404 && http.status !== 403)) throw error;
        }
      }
    }
    if (!this.alive(session.run)) throw new E2eeTrustError("not_ready");

    // 2. Forget the pending namespace, take the next generation, register again. Only now does
    //    anything local change.
    this.session = null;
    this.store = null;
    this.directories.clear();
    this.setPending([]);
    this.setState({ status: "starting", userId, deviceId: null, generation: null, reason: null });
    await this.lockQuietly(session.handle);
    let registered: { handle: Handle; generation: number; deviceId: string; approved: boolean };
    try {
      await crypto.wipe(userId, session.generation);
      const generation = session.generation + 1;
      const handle = await crypto.unlock(userId, generation);
      registered = await this.register(userId, session.run, generation, handle, 0);
    } catch (error) {
      // Every other phone is revoked, so the next start registers this one as the root. Leave
      // an honest error state (the screens offer a retry, which is exactly that start).
      if (this.alive(session.run)) {
        const reason = nativeCode(error) ?? (isRetryLater(error) ? "network" : (apiCode(error) ?? "start_failed"));
        this.report("reset", reason);
        this.setState({ status: "error", reason });
      }
      throw error;
    }
    if (!this.alive(session.run)) {
      await this.lockQuietly(registered.handle);
      throw new E2eeTrustError("not_ready");
    }
    this.session = {
      userId,
      generation: registered.generation,
      deviceId: registered.deviceId,
      handle: registered.handle,
      run: session.run,
    };
    this.store = new AppStateStore(this.ports.plain, userId);
    await saveDeviceRecord(secure, userId, {
      generation: registered.generation,
      deviceId: registered.deviceId,
      approved: registered.approved,
    });

    // 3. This phone's earlier pending registration can never be used now (its store is gone).
    //    Best effort: if it stays, the person can refuse it from the prompt like any other.
    try {
      if (session.deviceId !== registered.deviceId) await api.revokeDeviceV2(session.deviceId);
    } catch (error) {
      this.report("reset_old_device", error);
    }

    if (!registered.approved) {
      // The server did not make this the root (a phone was approved in the meantime). Say so
      // and stop: the person decides whether to go round again.
      this.setState({ status: "pending_approval", deviceId: registered.deviceId, generation: registered.generation });
      throw new E2eeTrustError("reset_incomplete");
    }
    this.setState({ status: "ready", userId, deviceId: registered.deviceId, generation: registered.generation });
    void this.afterReady();
  }

  // ---- send ----------------------------------------------------------------------

  private readySession(): Session | null {
    return this.state.status === "ready" ? this.session : null;
  }

  private exclusive<T>(job: () => Promise<T>): Promise<T> {
    const result = this.sendTail.then(job, job);
    this.sendTail = result.catch(() => undefined);
    return result;
  }

  /**
   * Send a text message in a DM. Resolves `sent`, or `queued` when the message is
   * encrypted and safe on this phone but the network stopped delivery (it goes out on the
   * next replay). Rejects with `E2eeSendError` for anything that stops the send before a
   * message exists (not ready, a hard stop, the other person has no phone set up, offline
   * before the first encryption); the text is not stored anywhere in that case.
   */
  sendDm(input: { conversationId: string; peerUserId: string; body: string }): Promise<SendOutcome> {
    return this.exclusive(() => this.doSend(input));
  }

  private async doSend(input: { conversationId: string; peerUserId: string; body: string }): Promise<SendOutcome> {
    const body = input.body.trim();
    if (body.length === 0 || Array.from(body).length > MAX_DM_BODY_CHARS) throw new E2eeSendError("invalid_body");
    const session = this.readySession();
    const store = this.store;
    if (session === null || store === null) throw new E2eeSendError("not_ready");
    if (!(await store.hasRoom())) throw new E2eeSendError("rejected");

    const entry: OutboxEntry = {
      clientMessageId: this.ports.newId(),
      conversationId: input.conversationId,
      peerUserId: input.peerUserId,
      createdAtMs: this.ports.now(),
      attempts: 0,
      status: "pending",
      failure: null,
    };
    // The intent is recorded BEFORE anything is encrypted: if the app dies between the
    // core committing the ciphertext and the POST, the outbox still knows to finish it.
    await store.update((state) => {
      state.outbox.push(entry);
    });
    return this.attempt(session, store, entry, body, false);
  }

  /** The user's own retry of a message marked `failed`. */
  retrySend(clientMessageId: string): Promise<SendOutcome | null> {
    return this.exclusive(async () => {
      const session = this.readySession();
      const store = this.store;
      if (session === null || store === null) throw new E2eeSendError("not_ready");
      const entry = (await store.load()).outbox.find((item) => item.clientMessageId === clientMessageId);
      if (entry === undefined) return null;
      const body = await this.findOutgoingBody(session, entry);
      if (body === null) {
        await this.dropEntry(store, entry);
        return null;
      }
      await store.update(() => {
        entry.status = "pending";
        entry.failure = null;
      });
      this.emit({ type: "thread", conversationId: entry.conversationId });
      return this.attempt(session, store, entry, body, true);
    });
  }

  /** Finish every unacknowledged message: app start, reconnect, foreground. */
  replayOutbox(): Promise<void> {
    if (this.replaying !== null) return this.replaying;
    const promise = this.exclusive(async () => {
      const session = this.readySession();
      const store = this.store;
      if (session === null || store === null) return;
      const pending = (await store.load()).outbox.filter((entry) => entry.status === "pending");
      for (const entry of pending) {
        const body = await this.findOutgoingBody(session, entry);
        if (body === null) {
          // Never encrypted (the intent was recorded and the app died before the core ran):
          // there is no message to send and nothing was shown.
          await this.dropEntry(store, entry);
          continue;
        }
        try {
          const outcome = await this.attempt(session, store, entry, body, true);
          if (outcome.kind === "queued") break; // The network is still down; the rest would fail too.
        } catch (error) {
          // `attempt` already recorded why this one failed; carry on with the others.
          this.report("replay", error instanceof E2eeSendError ? error.code : error);
        }
      }
    }).finally(() => {
      if (this.replaying === promise) this.replaying = null;
    });
    this.replaying = promise;
    return promise;
  }

  private async dropEntry(store: AppStateStore, entry: OutboxEntry): Promise<void> {
    await store.update((state) => {
      state.outbox = state.outbox.filter((item) => item.clientMessageId !== entry.clientMessageId);
    });
    this.emit({ type: "thread", conversationId: entry.conversationId });
  }

  /**
   * Forget this app's own bookkeeping for one conversation: its unsent-message index and its
   * "couldn't decrypt" placeholders. It does NOT touch the encrypted native store (that is
   * `chirpCrypto.purgeConversation`, c452) and nothing calls it yet: it is the matching half
   * for whoever wires the local purge into the app lifecycle, so a purged conversation does
   * not leave ids behind here.
   */
  async forgetConversation(conversationId: string): Promise<void> {
    const store = this.store;
    if (store === null) return;
    await store.update((state) => {
      state.outbox = state.outbox.filter((entry) => entry.conversationId !== conversationId);
      delete state.undecryptable[conversationId];
    });
    this.emit({ type: "thread", conversationId });
  }

  /** The plaintext of a message this device sent, read back from the encrypted store. */
  private async findOutgoingBody(session: Session, entry: OutboxEntry): Promise<string | null> {
    let before: number | null = null;
    for (let page = 0; page < 5; page++) {
      const rows = await this.ports.crypto.listMessages(session.handle, entry.conversationId, before, 200);
      for (const row of rows) {
        if (row.direction === "outgoing" && row.clientMessageId === entry.clientMessageId) return row.body;
      }
      if (rows.length < 200) return null;
      before = rows[rows.length - 1].seq;
    }
    return null;
  }

  /**
   * One delivery attempt: directories and trust, claim keys where a recipient has no
   * session, encrypt (idempotent on the client message id), POST the legs. A mismatch of
   * the device list, or a claim that finds a device gone, refreshes everything and goes
   * round once more with the SAME client message id: the core keeps the legs it already
   * made and adds only the missing ones.
   */
  private async attempt(
    session: Session,
    store: AppStateStore,
    entry: OutboxEntry,
    body: string,
    replay: boolean,
  ): Promise<SendOutcome> {
    const priorAttempts = entry.attempts;
    await store.update(() => {
      entry.attempts += 1;
    });
    let committed = replay; // Has the core committed ciphertext for this message yet?
    for (let round = 0; ; round++) {
      try {
        const directories = await this.prepare(session, entry.peerUserId, round > 0);
        const legs = await this.encryptFor(session, directories, entry, body);
        if (!committed) {
          committed = true;
          this.emit({ type: "thread", conversationId: entry.conversationId });
        }
        await this.ports.api.sendMessageV2(entry.conversationId, {
          sender_device_id: session.deviceId,
          client_message_id: entry.clientMessageId,
          envelope_version: E2EE_ENVELOPE_VERSION,
          legs: legs.map(legBody),
        });
        await this.dropEntry(store, entry);
        return { kind: "sent", clientMessageId: entry.clientMessageId };
      } catch (error) {
        if (round === 0 && (isConflict(error, "device_list_mismatch") || isConflict(error, "device_unavailable"))) {
          continue;
        }
        return this.settle(session, store, entry, error, committed, priorAttempts > 0);
      }
    }
  }

  private async encryptFor(session: Session, directories: DirectorySet, entry: OutboxEntry, body: string): Promise<Leg[]> {
    const { crypto, api } = this.ports;
    const recipients = [...directories.peer, ...directories.mine];
    const claimed = new Map<string, SignedKey>();
    const needKey: DirectoryDevice[] = [];
    for (const device of recipients) {
      if (!(await crypto.hasSession(session.handle, device.identityCurve25519))) needKey.push(device);
    }
    if (needKey.length > 0) {
      const out = await api.claimKeysV2(needKey.map((device) => device.deviceId));
      for (const key of out.keys) claimed.set(key.device_id, toSignedKey(key));
    }
    return crypto.encrypt(
      session.handle,
      entry.conversationId,
      entry.clientMessageId,
      body,
      directories.me,
      recipients.map((device) => ({ device, claimedKey: claimed.get(device.deviceId) ?? null })),
    );
  }

  /**
   * An attempt failed for good, or for now. Decide which, keep the outbox and the thread
   * honest, and either return `queued` or throw the typed reason.
   *
   * `committed` says whether a message exists (the core has encrypted it): then it stays in
   * the thread and the entry is kept (retry later) or marked `failed` (retrying will not
   * help). If nothing was encrypted yet there is nothing to keep, so the entry is removed.
   */
  private async settle(
    session: Session,
    store: AppStateStore,
    entry: OutboxEntry,
    error: unknown,
    committed: boolean,
    wasReplay: boolean,
  ): Promise<SendOutcome> {
    const conversationId = entry.conversationId;
    const queued = (): SendOutcome => {
      this.emit({ type: "thread", conversationId });
      return { kind: "queued", clientMessageId: entry.clientMessageId };
    };
    const refuse = async (typed: E2eeSendError): Promise<never> => {
      if (committed) {
        // A message exists in the thread but can never be delivered as it stands: say so.
        await store.update(() => {
          entry.status = "failed";
          entry.failure = typed.code;
        });
        this.emit({ type: "thread", conversationId });
      } else {
        await this.dropEntry(store, entry);
      }
      throw typed;
    };

    if (error instanceof E2eeSendError) return refuse(error);

    const native = nativeCode(error);
    const http = asApiError(error);
    if (native === "trust_hard_stop") {
      // The core saw a change the cached directory had not shown us.
      const trust = await this.trustFor(entry.peerUserId);
      return refuse(new E2eeSendError("blocked", trust.block));
    }
    if (native === "body_invalid" || native === "body_too_large") return refuse(new E2eeSendError("invalid_body"));
    if (native === "locked" || native === "e2ee_unavailable") {
      // The store was locked under us (sign-out mid-send). What is committed stays in the
      // outbox for the next sign-in; nothing is lost and nothing is claimed sent.
      if (committed) return queued();
      return refuse(new E2eeSendError("not_ready"));
    }
    if (native !== null) return refuse(new E2eeSendError("rejected"));

    if (http !== null) {
      if (isForbidden(error, "recipient_not_reachable") || isForbidden(error, "not_a_member")) {
        return refuse(new E2eeSendError("not_allowed"));
      }
      // c436: an account in this conversation (yours, or for a send any participant's) is being
      // deleted, and every v2 write is refused with 423 until it is gone. Not retried: a
      // message that already exists is marked failed (and the replay loop leaves failed ones
      // alone), one that was never encrypted is dropped.
      if (isAccountDeleting(error)) return refuse(new E2eeSendError("account_deletion"));
      if (isDeviceUnusable(error)) {
        this.markDeviceUnusable(session, http.detail);
        return refuse(new E2eeSendError("device_unusable"));
      }
      if (isConflict(error, "no_recipient_devices")) return refuse(new E2eeSendError("peer_no_devices"));
      if (isConflict(error, "client_message_id_reused") && wasReplay) {
        // An earlier attempt of THIS message reached the server (its reply was lost) and a
        // device was added since, so these legs differ from the stored ones. The message is
        // there; the new device simply has no leg, like any message that predates it.
        await this.dropEntry(store, entry);
        return { kind: "sent", clientMessageId: entry.clientMessageId };
      }
      if (!isRetryLater(error)) return refuse(new E2eeSendError("rejected"));
    }

    // No answer at all, or a server asking for patience: not the message's fault. A message
    // that exists waits in the outbox; one that does not is refused with the honest reason
    // (no network at all, or a server that answered but is busy).
    if (committed) return queued();
    return refuse(new E2eeSendError(http === null ? "offline" : "server_busy"));
  }

  /** A 403 told us THIS phone's device cannot act: revoked, or not (yet) approved. */
  private markDeviceUnusable(session: Session, detail: string): void {
    if (this.session !== session) return;
    if (detail === "device_not_approved") this.setState({ status: "pending_approval" });
    else this.setState({ status: "error", reason: detail });
  }

  // ---- receive -------------------------------------------------------------------

  /**
   * Fetch this device's legs for a conversation, decrypt anything not yet seen in server
   * order, and leave the plaintext in the native store. A hint that arrives while a sync is
   * running makes it run once more rather than being dropped.
   */
  syncConversation(conversationId: string, peerUserId: string): Promise<SyncResult> {
    const running = this.syncing.get(conversationId);
    if (running) {
      running.again = true;
      return running.promise;
    }
    const slot: { promise: Promise<SyncResult>; again: boolean } = { promise: Promise.resolve({ received: 0, undecryptable: 0 }), again: false };
    // Registered before the work starts, so the cleanup below always finds it.
    this.syncing.set(conversationId, slot);
    slot.promise = (async () => {
      const total: SyncResult = { received: 0, undecryptable: 0 };
      try {
        do {
          slot.again = false;
          const session = this.readySession();
          if (session === null) break;
          const result = await this.doSync(session, conversationId, peerUserId);
          total.received += result.received;
          total.undecryptable += result.undecryptable;
        } while (slot.again);
      } finally {
        if (this.syncing.get(conversationId) === slot) this.syncing.delete(conversationId);
      }
      return total;
    })();
    return slot.promise;
  }

  /** A live message hint: sync the conversation if it is a DM we can read. */
  async syncFromHint(conversationId: string): Promise<void> {
    const session = this.readySession();
    if (session === null) return;
    try {
      const peer = await this.ports.conversations.getPeer(conversationId, session.userId);
      if (peer.kind !== "dm" || peer.peerUserId === null) return;
      await this.syncConversation(conversationId, peer.peerUserId);
    } catch (error) {
      // A hint is only a reason to look. Offline now, or a transient fault: the next hint,
      // thread open or reconnect looks again, and nothing already received is lost.
      this.report("syncFromHint", error);
    }
  }

  private async doSync(session: Session, conversationId: string, peerUserId: string): Promise<SyncResult> {
    const { crypto } = this.ports;
    const store = this.store;
    if (store === null) return { received: 0, undecryptable: 0 };

    let mine = await this.directory(session.userId, false);
    let peer = await this.directory(peerUserId, false);
    // Receiving never checks trust (a changed identity blocks sending, not reading), but
    // the core still wants the directory seen so a sender's standing is recorded.
    await this.observeTrust(session, [
      { userId: peerUserId, devices: peer },
      { userId: session.userId, devices: mine },
    ]);

    const local = await crypto.listMessages(session.handle, conversationId, null, 200);
    const known = new Set<string>();
    let anchor = 0;
    for (const message of local) {
      if (message.messageId !== null) known.add(message.messageId);
      if (message.seq > anchor) anchor = message.seq;
    }
    const failures = (await store.load()).undecryptable[conversationId] ?? [];
    for (const failure of failures) known.add(failure.messageId);

    const unseen = await this.collectUnseen(session, conversationId, known);
    const result: SyncResult = { received: 0, undecryptable: 0 };
    if (unseen.length === 0) return result;

    let refreshed = false;
    let sawPrekey = false;
    for (const row of unseen) {
      let sender = [...peer, ...mine].find((device) => device.deviceId === row.sender_device_id);
      if (sender === undefined && !refreshed) {
        refreshed = true;
        mine = await this.directory(session.userId, true);
        peer = await this.directory(peerUserId, true);
        sender = [...peer, ...mine].find((device) => device.deviceId === row.sender_device_id);
      }
      const olmType = row.leg.olm_type;
      let code: string | null = null;
      if (sender === undefined) {
        code = "sender_unknown";
      } else if (olmType !== 0 && olmType !== 1) {
        code = "invalid_input";
      } else {
        try {
          const message = await crypto.decrypt(
            session.handle,
            row.id,
            { recipientDeviceId: session.deviceId, olmType, ciphertext: row.leg.ciphertext_b64 },
            sender,
            {
              conversationId,
              clientMessageId: row.client_message_id,
              senderUserId: sender.userId,
              senderDeviceId: sender.deviceId,
              recipientUserId: session.userId,
              recipientDeviceId: session.deviceId,
            },
          );
          result.received += 1;
          if (olmType === 0) sawPrekey = true;
          if (message.seq > anchor) anchor = message.seq;
          continue;
        } catch (error) {
          const native = nativeCode(error);
          if (!isPermanentDecryptFailure(native)) throw error; // About the device, not the message.
          code = native;
        }
      }
      // This message will never decrypt: remember a placeholder so it is not retried on
      // every sync and the thread can show that something arrived.
      const placeholder: UndecryptableRow = {
        messageId: row.id,
        createdAt: row.created_at,
        senderDeviceId: row.sender_device_id,
        code: code ?? "decrypt_failed",
        anchorSeq: anchor,
      };
      await store.update((state) => {
        const rows = (state.undecryptable[conversationId] ??= []);
        if (!rows.some((item) => item.messageId === placeholder.messageId)) rows.push(placeholder);
      });
      result.undecryptable += 1;
    }

    if (result.received + result.undecryptable > 0) this.emit({ type: "thread", conversationId });
    if (sawPrekey) void this.maintainKeys().catch(() => undefined);
    return result;
  }

  /** Walk history newest-first until a page is all known; return the unseen rows, oldest first. */
  private async collectUnseen(session: Session, conversationId: string, known: Set<string>): Promise<MessageHistoryV2Out[]> {
    const collected: MessageHistoryV2Out[] = [];
    let before: string | undefined;
    let beforeId: string | undefined;
    for (;;) {
      let page: MessageHistoryV2Out[];
      try {
        page = await this.ports.api.listMessagesV2(conversationId, session.deviceId, { limit: HISTORY_PAGE, before, beforeId });
      } catch (error) {
        if (isDeviceUnusable(error)) this.markDeviceUnusable(session, apiCode(error) ?? "device_unusable");
        throw error;
      }
      let unseenInPage = 0;
      for (const row of page) {
        if (known.has(row.id)) continue;
        collected.push(row);
        unseenInPage += 1;
      }
      if (page.length < HISTORY_PAGE || unseenInPage === 0 || collected.length >= MAX_SYNC_BACKLOG) break;
      const last = page[page.length - 1];
      before = last.created_at;
      beforeId = last.id;
    }
    return collected.reverse();
  }

  // ---- reading -------------------------------------------------------------------

  /** What a thread needs to draw: a page of the encrypted local store plus the app-side extras. */
  async loadThread(conversationId: string, beforeSeq: number | null = null): Promise<ThreadSnapshot> {
    const session = this.session;
    const store = this.store;
    if (session === null || store === null) return { messages: [], hasOlder: false, outbox: [], undecryptable: [] };
    const messages = await this.ports.crypto.listMessages(session.handle, conversationId, beforeSeq, LOCAL_PAGE);
    const state: AppState = await store.load();
    return {
      messages,
      hasOlder: messages.length === LOCAL_PAGE,
      outbox: state.outbox.filter((entry) => entry.conversationId === conversationId),
      undecryptable: [...(state.undecryptable[conversationId] ?? [])],
    };
  }
}

/** Internal: the run that asked for this work has been replaced or stopped. */
class Superseded extends Error {
  constructor() {
    super("superseded");
    this.name = "Superseded";
  }
}

/** Internal: a start that failed for a reason with its own fixed code. */
class StartFailure extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(reason);
    this.name = "StartFailure";
    this.reason = reason;
  }
}
