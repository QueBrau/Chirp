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
  isForbidden,
  isPermanentDecryptFailure,
  isRetryLater,
  nativeCode,
  staleGenerationMax,
} from "./e2eeErrors";
import { legBody, registrationBody, toDirectoryDevice, toSignedKey, uploadBody } from "./e2eeMapping";
import {
  AppStateStore,
  loadDeviceRecord,
  saveDeviceRecord,
  type KvPort,
} from "./e2eeStore";
import {
  E2EE_ENVELOPE_VERSION,
  E2eeSendError,
  HISTORY_PAGE,
  INACTIVE_STATE,
  INITIAL_ONE_TIME_KEYS,
  KEY_LOW_WATER,
  KEY_TARGET,
  MAX_DM_BODY_CHARS,
  MAX_SYNC_BACKLOG,
  type AppState,
  type DmTrust,
  type E2eeState,
  type OutboxEntry,
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

export type EngineEvent = { type: "state" } | { type: "thread"; conversationId: string };

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
  private store: AppStateStore | null = null;
  private readonly directories = new Map<string, { at: number; devices: DirectoryDevice[] }>();

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
    this.emit({ type: "state" });
    return this.state;
  }

  private alive(run: number): boolean {
    return this.run === run;
  }

  /** Tell the diagnostics port about a failure that is being absorbed. Codes only. */
  private report(where: string, error: unknown): void {
    try {
      this.ports.report?.(where, typeof error === "string" ? error : (nativeCode(error) ?? apiCode(error) ?? "error"));
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
   */
  start(userId: string): Promise<E2eeState> {
    if (this.starting !== null && this.startedFor === userId) return this.starting;
    if (this.startedFor === userId && this.state.status !== "error") return Promise.resolve(this.state);
    if (this.startedFor !== null && this.startedFor !== userId) void this.stop();
    const run = ++this.run;
    this.startedFor = userId;
    // A new bring-up never overlaps an older one still unwinding: two would race for the
    // same store ("store_in_use") and one handle would leak.
    const earlier = this.unwinding;
    const promise = earlier
      .then(() => this.bringUp(userId, run))
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
    this.setState({ ...INACTIVE_STATE });
    const locking = session ? this.lockQuietly(session.handle) : Promise.resolve();
    this.unwinding = Promise.all([this.unwinding, locking]).then(() => undefined);
    await locking;
  }

  /**
   * Ask again where this phone stands: a pending phone may have been approved; a failed
   * start is tried again. Cheap enough to call when the app returns to the foreground.
   */
  async recheck(): Promise<E2eeState> {
    const { status, userId } = this.state;
    if (userId === null) return this.state;
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

  private async bringUp(userId: string, run: number): Promise<E2eeState> {
    this.setState({ status: "starting", userId, deviceId: null, generation: null, reason: null });
    if (!this.ports.available()) {
      return this.setState({ status: "unavailable" });
    }
    const { crypto, secure } = this.ports;

    const record = await loadDeviceRecord(secure, userId);
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
        const registered = await this.register(userId, run, generation, handle);
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
    const reason = error instanceof StartFailure ? error.reason : (nativeCode(error) ?? apiCode(error) ?? "start_failed");
    this.report("start", reason);
    return this.setState({ status: "error", reason });
  }

  /** Register this phone's device. The first attempt may use an already-unlocked store. */
  private async register(userId: string, run: number, startGeneration: number, firstHandle: Handle) {
    const { crypto, api, secure } = this.ports;
    let generation = startGeneration;
    let handle = firstHandle;
    for (let attempt = 0; ; attempt++) {
      const created = await crypto.createDevice(handle, INITIAL_ONE_TIME_KEYS);
      // Before the request: from here a crash leaves a store with a device and no id, which
      // the next start knows how to clean up.
      await saveDeviceRecord(secure, userId, { generation, deviceId: null, approved: false });
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
        await saveDeviceRecord(secure, userId, { generation, deviceId: null, approved: false });
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

  private async directory(userId: string, force: boolean): Promise<DirectoryDevice[]> {
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
      if (isRetryLater(error) && cached) return cached.devices;
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

  private async observeTrust(
    session: Session,
    groups: { userId: string; devices: DirectoryDevice[] }[],
  ): Promise<TrustBlock | null> {
    let block: TrustBlock | null = null;
    for (const group of groups) {
      if (group.devices.length === 0) continue;
      const state: TrustState = await this.ports.crypto.observeDirectory(session.handle, group.userId, group.devices);
      if (state !== "trusted" && block === null) block = { userId: group.userId, reason: state };
    }
    return block;
  }

  /**
   * Whether the conversation with `peerUserId` is clear to send to, for the banner. Looks at
   * the directories (cached if the network is down). Never throws: when nothing can be
   * known, nothing is claimed.
   */
  async trustFor(peerUserId: string): Promise<DmTrust> {
    const session = this.readySession();
    if (session === null) return { block: null };
    try {
      const [mine, peer] = await Promise.all([this.directory(session.userId, false), this.directory(peerUserId, false)]);
      return { block: await this.observeTrust(session, [{ userId: peerUserId, devices: peer }, { userId: session.userId, devices: mine }]) };
    } catch (error) {
      this.report("trustFor", error);
      return { block: null };
    }
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
      if (isForbidden(error, "recipient_not_reachable")) return refuse(new E2eeSendError("not_allowed"));
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

    // No answer at all, or a server asking for patience: not the message's fault.
    if (committed) return queued();
    return refuse(new E2eeSendError("offline"));
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
