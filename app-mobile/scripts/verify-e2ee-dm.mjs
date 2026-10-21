/**
 * Executed regressions for the encrypted-DM engine (board c451, E2EE milestone M4).
 *
 *   npm run verify:e2ee-dm
 *
 * The real TypeScript in src/crypto/ (engine, mapping, store, thread builder) is transpiled
 * and run here against FAKES of its two boundaries: a stand-in for the native core and an
 * in-memory server that speaks the v2 API's real error shapes. No device, network, emulator
 * or credentials, and no test dependency (the repo has none). What this proves is the
 * PROTOCOL LOGIC: which calls are made, in what order, on which error. What it does not
 * prove is the cryptography (that is the Rust suite, 90 host tests) or the native bridge
 * (that is the two-simulator run in the c451 report). The fake core keeps only the
 * semantics the engine depends on, each taken from rust/src/core.rs:
 *   - one open handle per namespace (`store_in_use`), `wipe` closes it;
 *   - `encrypt` needs a claimed key for a recipient with no session, is idempotent on the
 *     client message id and appends only the legs a grown recipient set is missing, and
 *     refuses with `trust_hard_stop` while any involved user is not trusted;
 *   - `decrypt` is idempotent on the server message id, and a leg that cannot be read is a
 *     permanent per-message failure while a storage fault is not.
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const ROOT = fileURLToPath(new URL("../", import.meta.url));

// ---------------------------------------------------------------------------
// Loader: transpile src/crypto/*.ts to CommonJS and run it in this realm.
// ---------------------------------------------------------------------------

const modules = new Map();
function load(path) {
  const cached = modules.get(path);
  if (cached) return cached.exports;
  const output = ts.transpileModule(readFileSync(path, "utf8"), {
    fileName: path,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  modules.set(path, module);
  new Function("require", "module", "exports", output)((specifier) => {
    if (!specifier.startsWith(".")) throw new Error(`unexpected import of ${specifier} from ${path}`);
    const base = resolve(dirname(path), specifier);
    const found = [`${base}.ts`, `${base}.tsx`].find((candidate) => existsSync(candidate));
    if (!found) throw new Error(`missing module ${specifier} imported by ${path}`);
    return load(found);
  }, module, module.exports);
  return module.exports;
}
const src = (name) => load(resolve(ROOT, "src/crypto", `${name}.ts`));
const { E2eeEngine } = src("e2eeEngine");
const { E2eeSendError, KEY_LOW_WATER, KEY_TARGET } = src("e2eeTypes");
const { buildThread } = src("e2eeThread");
const { loadDeviceRecord } = src("e2eeStore");
const { isRetryLater, asApiError } = src("e2eeErrors");

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

class FakeNativeError extends Error {
  constructor(code) {
    super(code);
    this.name = "E2EEError";
    this.code = code;
  }
}
function apiError(status, detail, payload = null) {
  const error = new Error(`API ${status}: ${detail}`);
  error.name = "ApiError";
  error.status = status;
  error.detail = detail;
  error.payload = payload ? { detail, ...payload } : { detail };
  return error;
}
const networkError = () => Object.assign(new TypeError("Network request failed"), { name: "TypeError" });

let idCounter = 0;
const uuid = () => `00000000-0000-4000-8000-${String(++idCounter).padStart(12, "0")}`;

function memoryKv() {
  const map = new Map();
  return {
    map,
    async get(key) { return map.has(key) ? map.get(key) : null; },
    async set(key, value) { map.set(key, value); },
    async remove(key) { map.delete(key); },
  };
}

/** The native core, reduced to the behaviour the engine relies on. One per PHONE (it is the disk). */
class FakeCore {
  constructor() {
    this.stores = new Map(); // "user:gen" -> store
    this.open = new Map(); // handle -> store
    this.calls = [];
    this.fault = null; // a native code to throw from the next `decrypt`
    this.counter = 0;
  }
  log(name, ...args) { this.calls.push([name, ...args]); }
  count(name) { return this.calls.filter((c) => c[0] === name).length; }
  /** Process death: every handle is gone, the stores (disk) stay. */
  kill() { for (const store of this.stores.values()) store.handle = null; this.open.clear(); }
  store(handle) {
    const store = this.open.get(handle);
    if (!store) throw new FakeNativeError("locked");
    return store;
  }
  async unlock(userId, generation) {
    this.log("unlock", userId, generation);
    const key = `${userId}:${generation}`;
    let store = this.stores.get(key);
    if (!store) {
      store = { key, userId, generation, device: null, keys: [], sessions: new Set(), outbox: new Map(), messages: [], seq: 0,
        decrypted: new Map(), pins: new Map(), trust: new Map(), handle: null, nextKey: 1 };
      this.stores.set(key, store);
    }
    if (store.handle) throw new FakeNativeError("store_in_use");
    const handle = `h${++this.counter}`;
    store.handle = handle;
    this.open.set(handle, store);
    return handle;
  }
  async lock(handle) {
    this.log("lock", handle);
    const store = this.open.get(handle);
    if (store) store.handle = null;
    this.open.delete(handle);
  }
  async wipe(userId, generation) {
    this.log("wipe", userId, generation);
    const key = `${userId}:${generation}`;
    const store = this.stores.get(key);
    if (store?.handle) this.open.delete(store.handle);
    this.stores.delete(key);
  }
  async publicIdentity(handle) {
    const { device } = this.store(handle);
    return device ? { curve25519: device.curve, ed25519: device.ed } : null;
  }
  newKeys(store, kind, n) {
    return Array.from({ length: n }, () => {
      const key = { keyId: store.nextKey++, kind, publicKey: `pk-${store.key}-${store.nextKey}`, signature: "sig", published: false };
      store.keys.push(key);
      return { keyId: key.keyId, kind, publicKey: key.publicKey, signature: key.signature };
    });
  }
  async createDevice(handle, count) {
    this.log("createDevice", count);
    const store = this.store(handle);
    if (store.device) throw new FakeNativeError("device_exists");
    store.device = { curve: `curve-${store.key}`, ed: `ed-${store.key}`, serverId: null };
    const oneTimeKeys = this.newKeys(store, "one_time", count);
    const [fallbackKey] = this.newKeys(store, "fallback", 1);
    return { identity: { curve25519: store.device.curve, ed25519: store.device.ed }, bindingSignature: "binding", oneTimeKeys, fallbackKey };
  }
  async markPublished(handle, serverId, ids) {
    this.log("markPublished", serverId, ids.length);
    const store = this.store(handle);
    store.device.serverId = serverId;
    for (const key of store.keys) if (ids.includes(key.keyId)) key.published = true;
  }
  async pendingKeys(handle) {
    return this.store(handle).keys.filter((k) => !k.published).map((k) => ({ keyId: k.keyId, kind: k.kind, publicKey: k.publicKey, signature: k.signature }));
  }
  async topUpKeys(handle, count) {
    this.log("topUpKeys", count);
    return this.newKeys(this.store(handle), "one_time", count);
  }
  evaluate(store, userId, devices) {
    const pins = store.pins.get(userId);
    if (!pins) {
      return devices.filter((d) => d.approval === null).length === 1 ? "trusted" : "unapproved_device";
    }
    if (devices.some((d) => pins.has(d.deviceId) && pins.get(d.deviceId) !== d.identityEd25519)) return "identity_changed";
    if (devices.some((d) => !pins.has(d.deviceId) && d.approval === null)) return "unapproved_device";
    return "trusted";
  }
  async observeDirectory(handle, userId, devices) {
    this.log("observeDirectory", userId, devices.length);
    const store = this.store(handle);
    const state = this.evaluate(store, userId, devices);
    store.trust.set(userId, state);
    if (state === "trusted") store.pins.set(userId, new Map(devices.map((d) => [d.deviceId, d.identityEd25519])));
    return state;
  }
  async hasSession(handle, curve) {
    this.log("hasSession", curve);
    return this.store(handle).sessions.has(curve);
  }
  async encrypt(handle, conversationId, clientMessageId, body, sender, recipients) {
    this.log("encrypt", clientMessageId, recipients.map((r) => r.device.deviceId), recipients.map((r) => r.claimedKey ? 1 : 0));
    const store = this.store(handle);
    const groups = new Map();
    for (const r of recipients) groups.set(r.device.userId, [...(groups.get(r.device.userId) ?? []), r.device]);
    groups.set(sender.userId, [...(groups.get(sender.userId) ?? []), sender]);
    for (const [userId, devices] of groups) {
      if (this.evaluate(store, userId, devices) !== "trusted") throw new FakeNativeError("trust_hard_stop");
    }
    let entry = store.outbox.get(clientMessageId);
    if (entry && (entry.body !== body || entry.conversationId !== conversationId)) throw new FakeNativeError("client_message_id_reused");
    if (!entry) {
      entry = { body, conversationId, legs: new Map() };
      store.outbox.set(clientMessageId, entry);
      store.messages.push({ seq: ++store.seq, direction: "outgoing", messageId: null, conversationId, clientMessageId,
        senderUserId: sender.userId, senderDeviceId: sender.deviceId, kind: "text", body, senderTrust: "trusted", storedAtMs: 1000 + store.seq });
    }
    for (const r of recipients) {
      if (entry.legs.has(r.device.deviceId)) continue;
      if (!store.sessions.has(r.device.identityCurve25519)) {
        if (!r.claimedKey) throw new FakeNativeError("missing_claimed_key");
        store.sessions.add(r.device.identityCurve25519);
        entry.legs.set(r.device.deviceId, { olmType: 0 });
      } else {
        entry.legs.set(r.device.deviceId, { olmType: 1 });
      }
    }
    return recipients.map((r) => ({
      recipientDeviceId: r.device.deviceId,
      olmType: entry.legs.get(r.device.deviceId).olmType,
      ciphertext: Buffer.from(JSON.stringify({ conversationId, clientMessageId, to: r.device.deviceId, from: sender.deviceId, body })).toString("base64"),
    }));
  }
  async decrypt(handle, messageId, leg, sender, expected) {
    this.log("decrypt", messageId);
    const store = this.store(handle);
    if (store.decrypted.has(messageId)) return store.decrypted.get(messageId);
    if (this.fault) { const code = this.fault; this.fault = null; throw new FakeNativeError(code); }
    let parsed;
    try { parsed = JSON.parse(Buffer.from(leg.ciphertext, "base64").toString()); } catch { throw new FakeNativeError("decrypt_failed"); }
    if (parsed.to !== expected.recipientDeviceId || parsed.from !== expected.senderDeviceId || parsed.clientMessageId !== expected.clientMessageId) {
      throw new FakeNativeError("envelope_mismatch");
    }
    store.sessions.add(sender.identityCurve25519);
    const message = { seq: ++store.seq, direction: "incoming", messageId, conversationId: expected.conversationId,
      clientMessageId: expected.clientMessageId, senderUserId: sender.userId, senderDeviceId: sender.deviceId, kind: "text",
      body: parsed.body, senderTrust: "trusted", storedAtMs: 2000 + store.seq };
    store.messages.push(message);
    store.decrypted.set(messageId, message);
    return message;
  }
  async listMessages(handle, conversationId, before, limit) {
    return this.store(handle).messages
      .filter((m) => m.conversationId === conversationId && (before === null || m.seq < before))
      .sort((a, b) => b.seq - a.seq)
      .slice(0, limit);
  }
}

/** The v2 server: the real error shapes and rules of routers/keys_v2.py and messages_v2.py. */
class FakeServer {
  constructor() {
    this.devices = [];
    this.messages = [];
    this.down = false;
    // A server deployed before the v2 routes existed: every /v2 path is simply not there.
    this.noV2 = false;
    // Registration requests get no usable answer (the reply is lost on the way back).
    this.registerFails = false;
    this.calls = [];
    this.convMembers = new Map(); // conversation id -> [user ids]
    this.tick = 0;
  }
  log(name, ...args) { this.calls.push([name, ...args]); }
  count(name) { return this.calls.filter((c) => c[0] === name).length; }
  conversation(a, b) { const id = uuid(); this.convMembers.set(id, [a, b]); return id; }
  live(userId) { return this.devices.filter((d) => d.user_id === userId && d.approved && !d.revoked); }
  maxGeneration(userId) { return Math.max(0, ...this.devices.filter((d) => d.user_id === userId).map((d) => d.generation)); }
  approve(deviceId, approverId) {
    const device = this.devices.find((d) => d.id === deviceId);
    device.approved = true;
    device.approved_by = approverId;
  }
  apiFor(userId) {
    const guard = () => {
      if (this.down) throw networkError();
      if (this.noV2) throw apiError(404, "Not Found");
    };
    return {
      registerDeviceV2: async (body) => {
        this.log("register", body.generation); guard();
        if (this.registerFails) throw networkError();
        const max = this.maxGeneration(userId);
        if (body.generation <= max) throw apiError(409, "stale_generation", { max_generation: max });
        const device = { id: uuid(), user_id: userId, generation: body.generation, curve: body.identity_curve25519_b64, ed: body.identity_ed25519_b64,
          binding: body.binding_signature_b64, approved: this.live(userId).length === 0, approved_by: null, revoked: false,
          keys: [...body.one_time_keys.map((k) => ({ id: k.key_id, kind: "one_time", consumed: false })), { id: body.fallback_key.key_id, kind: "fallback", consumed: false }] };
        this.devices.push(device);
        return { id: device.id, user_id: userId, approved: device.approved, generation: device.generation };
      },
      uploadKeysV2: async (deviceId, body) => {
        this.log("upload", deviceId, body.one_time_keys.length); guard();
        const device = this.devices.find((d) => d.id === deviceId);
        for (const k of body.one_time_keys) if (device.keys.some((x) => x.id === k.key_id)) throw apiError(409, "key_id_reused");
        for (const k of body.one_time_keys) device.keys.push({ id: k.key_id, kind: "one_time", consumed: false });
        return { device_id: deviceId };
      },
      getKeyCountV2: async (deviceId) => {
        this.log("count", deviceId); guard();
        const device = this.devices.find((d) => d.id === deviceId);
        return { device_id: deviceId, one_time_keys_available: device.keys.filter((k) => k.kind === "one_time" && !k.consumed).length, fallback_keys_available: 1 };
      },
      getUserDirectoryV2: async (target) => {
        this.log("directory", target); guard();
        return { user_id: target, devices: this.live(target).map((d) => {
          const approver = d.approved_by ? this.devices.find((x) => x.id === d.approved_by) : null;
          return { device_id: d.id, suite: "vodozemac-olm-v1", generation: d.generation, identity_curve25519_b64: d.curve, identity_ed25519_b64: d.ed,
            binding_signature_b64: d.binding, created_at: "2026-10-08T00:00:00Z", approved_by_device_id: d.approved_by,
            approval_signature_b64: approver ? "approval" : null, approver_identity_ed25519_b64: approver ? approver.ed : null };
        }) };
      },
      claimKeysV2: async (ids) => {
        this.log("claim", ids.slice()); guard();
        const keys = [];
        for (const id of ids) {
          const device = this.devices.find((d) => d.id === id);
          if (!device || !device.approved || device.revoked) throw apiError(409, "device_unavailable", { device_ids: [id] });
          const otk = device.keys.find((k) => k.kind === "one_time" && !k.consumed);
          if (otk) otk.consumed = true;
          const used = otk ?? device.keys.find((k) => k.kind === "fallback");
          keys.push({ device_id: id, key_id: used.id, kind: used.kind, public_key_b64: "pk", signature_b64: "sig" });
        }
        return { keys };
      },
      sendMessageV2: async (conversationId, body) => {
        this.log("send", conversationId, body.client_message_id); guard();
        const sender = this.devices.find((d) => d.id === body.sender_device_id);
        if (!sender || sender.revoked) throw apiError(403, "device_revoked");
        if (!sender.approved) throw apiError(403, "device_not_approved");
        const legs = new Map(body.legs.map((l) => [l.recipient_device_id, l]));
        const existing = this.messages.find((m) => m.sender === sender.id && m.client === body.client_message_id);
        if (existing) {
          const same = existing.legs.size === legs.size && [...legs].every(([k, v]) => existing.legs.get(k)?.ciphertext_b64 === v.ciphertext_b64);
          if (same) return { id: existing.id };
          throw apiError(409, "client_message_id_reused");
        }
        const members = this.convMembers.get(conversationId);
        const required = members.flatMap((u) => this.live(u)).filter((d) => d.id !== sender.id).map((d) => d.id).sort();
        if (!members.filter((u) => u !== sender.user_id).flatMap((u) => this.live(u)).length) throw apiError(409, "no_recipient_devices", { device_ids: required });
        if (JSON.stringify([...legs.keys()].sort()) !== JSON.stringify(required)) throw apiError(409, "device_list_mismatch", { device_ids: required });
        const message = { id: uuid(), conversation: conversationId, sender: sender.id, client: body.client_message_id, legs, created_at: `2026-10-08T00:00:${String(++this.tick).padStart(2, "0")}.000000Z` };
        this.messages.push(message);
        return { id: message.id };
      },
      listMessagesV2: async (conversationId, deviceId, options = {}) => {
        this.log("history", conversationId, deviceId); guard();
        const device = this.devices.find((d) => d.id === deviceId);
        if (device.revoked) throw apiError(403, "device_revoked");
        if (!device.approved) throw apiError(403, "device_not_approved");
        const rows = this.messages.filter((m) => m.conversation === conversationId && m.legs.has(deviceId)).sort((a, b) => b.created_at.localeCompare(a.created_at));
        const start = options.before ? rows.findIndex((m) => m.created_at === options.before && m.id === options.beforeId) + 1 : 0;
        return rows.slice(start, start + (options.limit ?? 50)).map((m) => ({
          id: m.id, conversation_id: conversationId, sender_device_id: m.sender, client_message_id: m.client, envelope_version: 1, created_at: m.created_at,
          leg: { olm_type: m.legs.get(deviceId).olm_type, ciphertext_b64: m.legs.get(deviceId).ciphertext_b64 },
        }));
      },
    };
  }
}

/** One phone: its disk (core store + both key-value stores) and an engine that can be rebuilt (a restart). */
class Phone {
  constructor(world, userId) {
    this.world = world;
    this.userId = userId;
    this.core = new FakeCore();
    this.secure = memoryKv();
    this.plain = memoryKv();
    this.available = true;
    this.reports = [];
    this.engine = this.newEngine();
  }
  newEngine() {
    return new E2eeEngine({
      crypto: this.core,
      api: this.world.server.apiFor(this.userId),
      secure: this.secure,
      plain: this.plain,
      available: () => this.available,
      newId: uuid,
      now: () => this.world.now,
      report: (where, code) => this.reports.push([where, code]),
      conversations: { getPeer: async (conversationId, me) => {
        const members = this.world.server.convMembers.get(conversationId);
        return { kind: "dm", peerUserId: members.find((u) => u !== me) ?? null };
      } },
    });
  }
  /** Kill the app and open it again: the disk survives, memory does not. */
  async restart() {
    this.core.kill();
    this.engine = this.newEngine();
    return this.engine.start(this.userId);
  }
}

class World {
  constructor() { this.server = new FakeServer(); this.now = 1_000_000; }
  phone(userId) { return new Phone(this, userId); }
  async pair() {
    const alice = this.phone(uuid()), bob = this.phone(uuid());
    await alice.engine.start(alice.userId);
    await bob.engine.start(bob.userId);
    const conversation = this.server.conversation(alice.userId, bob.userId);
    return { alice, bob, conversation };
  }
}

const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); };
const bodies = (core, conversation) => core.stores.size === 0 ? [] : [...core.stores.values()].flatMap((s) => s.messages).filter((m) => m.conversationId === conversation).sort((a, b) => a.seq - b.seq).map((m) => `${m.direction === "outgoing" ? ">" : "<"}${m.body}`);

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

const cases = [];
const test = (name, fn) => cases.push({ name, fn });

test("first sign-in registers a root device, publishes its keys, and is ready", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  const state = await phone.engine.start(phone.userId);
  await flush();
  assert.equal(state.status, "ready");
  assert.equal(world.server.count("register"), 1);
  const record = await loadDeviceRecord(phone.secure, phone.userId);
  assert.deepEqual({ generation: record.generation, approved: record.approved, deviceId: record.deviceId }, { generation: 1, approved: true, deviceId: state.deviceId });
  const store = [...phone.core.stores.values()][0];
  assert.ok(store.keys.every((k) => k.published), "every key the server holds is confirmed to the core");
  assert.equal(store.device.serverId, state.deviceId);
});

test("stale_generation: wipe the unregistered namespace, retry once with max+1, surface pending_approval (never loop)", async () => {
  const world = new World();
  const userId = uuid();
  // The account already has an approved device from an earlier install.
  const old = world.phone(userId);
  await old.engine.start(userId);
  const registeredBefore = world.server.count("register");
  const fresh = world.phone(userId); // a new install: no store, no record
  const state = await fresh.engine.start(userId);
  assert.equal(state.status, "pending_approval");
  assert.equal(state.generation, 2);
  assert.equal(world.server.count("register") - registeredBefore, 2, "one refused attempt, one accepted: no loop");
  assert.deepEqual(fresh.core.calls.filter((c) => c[0] === "wipe"), [["wipe", userId, 1]]);
  assert.equal((await loadDeviceRecord(fresh.secure, userId)).generation, 2);
  assert.equal(world.server.live(userId).length, 1, "the pending device is not in the directory");
  assert.equal(fresh.core.count("createDevice"), 2, "the wiped store's device is never registered");
});

test("stale_generation twice in a row is an error, not a loop", async () => {
  const world = new World();
  const userId = uuid();
  const phone = world.phone(userId);
  const api = phone.engine.ports.api;
  let attempts = 0;
  phone.engine.ports.api = { ...api, registerDeviceV2: async () => { attempts++; throw apiError(409, "stale_generation", { max_generation: 4 + attempts }); } };
  const state = await phone.engine.start(userId);
  assert.equal(state.status, "error");
  assert.equal(state.reason, "stale_generation");
  assert.equal(attempts, 2);
});

test("reinstall: the Keychain record outlives the files, so the new store skips the taken generation and stays pending", async () => {
  const world = new World();
  const userId = uuid();
  const phone = world.phone(userId);
  await phone.engine.start(userId);
  assert.equal((await loadDeviceRecord(phone.secure, userId)).generation, 1);
  // Delete the app: files go, the Keychain record stays.
  phone.core = new FakeCore();
  phone.plain = memoryKv();
  const again = phone.newEngine();
  const state = await again.start(userId);
  assert.equal(state.status, "pending_approval");
  assert.equal(state.generation, 2);
  assert.equal(world.server.count("register"), 2, "the first install's registration plus exactly one more: no refused attempt");
  assert.deepEqual(phone.core.calls.filter((c) => c[0] === "wipe"), [["wipe", userId, 1]], "the namespace the reinstall opened for the taken generation is cleaned up, Keychain items included");
});

test("a pending phone becomes ready when the other phone approves it", async () => {
  const world = new World();
  const userId = uuid();
  const first = world.phone(userId);
  await first.engine.start(userId);
  const second = world.phone(userId);
  assert.equal((await second.engine.start(userId)).status, "pending_approval");
  assert.equal((await second.engine.recheck()).status, "pending_approval", "still waiting");
  world.server.approve(second.engine.getState().deviceId, first.engine.getState().deviceId);
  assert.equal((await second.engine.recheck()).status, "ready");
  assert.equal((await loadDeviceRecord(second.secure, userId)).approved, true);
});

test("a crash after creating the device: recovered from the directory when it was registered, restarted when it was not", async () => {
  const world = new World();
  const userId = uuid();
  // (a) the POST reached the server but the app died before remembering the answer
  const a = world.phone(userId);
  const handle = await a.core.unlock(userId, 1);
  const created = await a.core.createDevice(handle, 5);
  const reply = await a.engine.ports.api.registerDeviceV2({ generation: 1, identity_curve25519_b64: created.identity.curve25519, identity_ed25519_b64: created.identity.ed25519,
    binding_signature_b64: "b", one_time_keys: created.oneTimeKeys.map((k) => ({ key_id: k.keyId })), fallback_key: { key_id: created.fallbackKey.keyId } });
  a.core.kill();
  await a.secure.set(`chirp.e2ee.device.${userId}`, JSON.stringify({ generation: 1, deviceId: null, approved: false }));
  const recovered = await a.newEngine().start(userId);
  assert.equal(recovered.status, "ready");
  assert.equal(recovered.deviceId, reply.id);
  assert.equal(world.server.count("register"), 1, "no second registration");
  assert.equal(a.core.count("wipe"), 0);
  // (b) the app died before the POST
  const user2 = uuid();
  const b = world.phone(user2);
  const h2 = await b.core.unlock(user2, 1);
  await b.core.createDevice(h2, 5);
  b.core.kill();
  await b.secure.set(`chirp.e2ee.device.${user2}`, JSON.stringify({ generation: 1, deviceId: null, approved: false }));
  const redone = await b.newEngine().start(user2);
  assert.equal(redone.status, "ready");
  assert.equal(b.core.count("wipe"), 1, "the unregistered store is dropped and made again");
});

test("keys: pending keys are re-uploaded on start; a low server count is topped up to the target; a healthy one is left alone", async () => {
  const world = new World();
  const userId = uuid();
  const phone = world.phone(userId);
  await phone.engine.start(userId);
  await flush();
  assert.equal(phone.core.count("topUpKeys"), 0, "100 available is above the low-water mark");
  // The server pool runs low.
  const device = world.server.devices[0];
  device.keys.filter((k) => k.kind === "one_time").slice(0, 100 - 10).forEach((k) => { k.consumed = true; });
  await phone.engine.maintainKeys();
  assert.deepEqual(phone.core.calls.filter((c) => c[0] === "topUpKeys"), [["topUpKeys", KEY_TARGET - 10]]);
  assert.equal(world.server.calls.filter((c) => c[0] === "upload").at(-1)[2], KEY_TARGET - 10);
  // A crash left keys generated but never confirmed: they are uploaded on the next start.
  const store = [...phone.core.stores.values()][0];
  const stranded = await phone.core.topUpKeys(store.handle, 3);
  assert.equal((await phone.core.pendingKeys(store.handle)).length, 3);
  phone.core.calls.length = 0;
  await phone.restart();
  await flush();
  assert.equal((await phone.core.pendingKeys([...phone.core.open.keys()][0])).length, 0);
  assert.ok(stranded.length === 3);
  // The server already has them (the upload landed, the confirmation was lost): treated as published, not an error.
  const again = await phone.core.topUpKeys([...phone.core.open.keys()][0], 2);
  device.keys.push(...again.map((k) => ({ id: k.keyId, kind: "one_time", consumed: false })));
  await phone.engine.maintainKeys();
  assert.equal((await phone.core.pendingKeys([...phone.core.open.keys()][0])).length, 0);
});

test("send: a key is claimed only for a recipient with no session; the second message claims nothing", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  const first = await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "hello" });
  assert.equal(first.kind, "sent");
  assert.deepEqual(world.server.calls.filter((c) => c[0] === "claim").map((c) => c[1].length), [1]);
  world.server.calls.length = 0;
  const second = await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "again" });
  assert.equal(second.kind, "sent");
  assert.equal(world.server.count("claim"), 0, "an established session needs no new key");
  assert.equal(world.server.count("send"), 1);
});

test("send with two phones on each side claims for every sessionless device, and only for new ones afterwards", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  const alice2 = world.phone(alice.userId);
  await alice2.engine.start(alice.userId);
  world.server.approve(alice2.engine.getState().deviceId, alice.engine.getState().deviceId);
  await alice2.engine.recheck();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "one" });
  assert.deepEqual(world.server.calls.filter((c) => c[0] === "claim").map((c) => c[1].length), [2], "bob's phone and alice's other phone");
  assert.equal(world.server.messages[0].legs.size, 2, "one leg per recipient device");
  // Bob adds a phone: only that one needs a key.
  const bob2 = world.phone(bob.userId);
  await bob2.engine.start(bob.userId);
  world.server.approve(bob2.engine.getState().deviceId, bob.engine.getState().deviceId);
  world.now += 60_000; // past the directory cache
  world.server.calls.length = 0;
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "two" });
  assert.deepEqual(world.server.calls.filter((c) => c[0] === "claim").map((c) => c[1].length), [1]);
});

test("device_list_mismatch: refresh, re-encrypt with the SAME client message id, post again - once", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "warm up" });
  // Bob adds a phone AFTER alice cached his directory, so her first attempt is one leg short.
  const bob2 = world.phone(bob.userId);
  await bob2.engine.start(bob.userId);
  world.server.approve(bob2.engine.getState().deviceId, bob.engine.getState().deviceId);
  world.server.calls.length = 0;
  alice.core.calls.length = 0;
  const outcome = await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "after" });
  assert.equal(outcome.kind, "sent");
  assert.equal(world.server.count("send"), 2, "refused once, accepted once");
  const encrypts = alice.core.calls.filter((c) => c[0] === "encrypt");
  assert.equal(encrypts.length, 2);
  assert.equal(encrypts[0][1], encrypts[1][1], "the same client message id both times");
  assert.deepEqual(world.server.calls.filter((c) => c[0] === "claim").map((c) => c[1].length), [1], "only the new device is claimed");
  assert.equal(world.server.messages.at(-1).legs.size, 2);
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  // And it does not spin: a server that keeps refusing is tried exactly twice, then reported.
  world.server.calls.length = 0;
  const real = alice.engine.ports.api.sendMessageV2;
  alice.engine.ports.api = { ...alice.engine.ports.api, sendMessageV2: async (...args) => { world.server.log("send"); throw apiError(409, "device_list_mismatch", { device_ids: [] }); } };
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "stubborn" }), (e) => e instanceof E2eeSendError && e.code === "rejected");
  assert.equal(world.server.count("send"), 2);
  const thread = await alice.engine.loadThread(conversation);
  assert.equal(thread.outbox[0].status, "failed", "a message that exists but cannot be delivered is marked, not hidden");
  void real;
});

test("outbox: a send while the API is down is kept, shown as sending, and goes out on replay without re-claiming or duplicating", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "established" });
  world.server.down = true;
  const outcome = await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "while offline" });
  assert.equal(outcome.kind, "queued");
  let thread = await alice.engine.loadThread(conversation);
  assert.equal(thread.outbox.length, 1);
  const items = buildThread({ ...thread, myUserId: alice.userId });
  assert.equal(items.at(-1).body, "while offline");
  assert.equal(items.at(-1).delivery, "sending");
  assert.equal(items.at(-1).mine, true);
  // The app is killed and reopened with the network still down: the intent survives, nothing is lost.
  world.server.down = false;
  await alice.restart();
  await flush();
  assert.equal(world.server.messages.length, 2, "the restart's replay delivered it");
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.equal(world.server.messages.filter((m) => m.client === thread.outbox[0].clientMessageId).length, 1, "exactly one server message");
  assert.equal(world.server.count("claim"), 1, "no key was claimed for the replay");
  // Bob can read it.
  await bob.engine.syncConversation(conversation, alice.userId);
  assert.deepEqual(bodies(bob.core, conversation), ["<established", "<while offline"]);
});

test("outbox: replay stops at the first message the network still refuses and keeps the order", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "x" });
  world.server.down = true;
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "first offline" });
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "second offline" });
  world.server.calls.length = 0;
  await alice.engine.replayOutbox();
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 2, "still down: both stay");
  assert.equal(world.server.count("send"), 1, "stopped at the first refusal instead of hammering a dead network");
  world.server.down = false;
  await alice.engine.replayOutbox();
  const sentIds = [...alice.core.stores.values()][0].messages.filter((m) => m.direction === "outgoing" && m.body.endsWith("offline")).map((m) => m.clientMessageId);
  assert.deepEqual(world.server.messages.slice(-2).map((m) => m.client), sentIds, "delivered in the order they were written");
  await bob.engine.syncConversation(conversation, alice.userId);
  assert.deepEqual(bodies(bob.core, conversation), ["<x", "<first offline", "<second offline"]);
});

test("offline before anything is encrypted: refused with 'offline', nothing stored, nothing queued", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  world.now += 60_000;
  world.server.down = true;
  alice.engine.directories.clear();
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "hi" }), (e) => e instanceof E2eeSendError && e.code === "offline");
  assert.equal(alice.core.count("encrypt"), 0);
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.deepEqual(bodies(alice.core, conversation), []);
});

test("a replay whose first attempt already reached the server is treated as sent, even after a device was added", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "warm" });
  // The POST lands but the reply is lost.
  const api = alice.engine.ports.api;
  alice.engine.ports.api = { ...api, sendMessageV2: async (...args) => { await api.sendMessageV2(...args); throw networkError(); } };
  assert.equal((await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "lost reply" })).kind, "queued");
  alice.engine.ports.api = api;
  // Bob adds a phone before the replay: the replay's legs now differ from the stored ones.
  const bob2 = world.phone(bob.userId);
  await bob2.engine.start(bob.userId);
  world.server.approve(bob2.engine.getState().deviceId, bob.engine.getState().deviceId);
  world.now += 60_000;
  await alice.engine.replayOutbox();
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.equal(world.server.messages.length, 2);
});

test("hard stop: a changed identity blocks SENDING with no encryption, no queue and no post, while receiving still works", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "before" });
  await bob.engine.syncConversation(conversation, alice.userId);
  await bob.engine.sendDm({ conversationId: conversation, peerUserId: alice.userId, body: "sent before the change" });
  // Bob's device keeps its id but its identity key changes (a different phone claiming the same device).
  world.server.devices.find((d) => d.user_id === bob.userId).ed = "ed-someone-else";
  world.now += 60_000;
  const trust = await alice.engine.trustFor(bob.userId);
  assert.deepEqual(trust.block, { userId: bob.userId, reason: "identity_changed" });
  alice.core.calls.length = 0;
  world.server.calls.length = 0;
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "blocked" }), (e) => e instanceof E2eeSendError && e.code === "blocked" && e.block.reason === "identity_changed");
  assert.equal(alice.core.count("encrypt"), 0);
  assert.equal(world.server.count("send"), 0);
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.deepEqual(bodies(alice.core, conversation), [">before"], "the refused text was never stored");
  // Receiving does not check trust: alice still reads what bob sent before the change.
  const result = await alice.engine.syncConversation(conversation, bob.userId);
  assert.equal(result.received, 1);
  assert.deepEqual(bodies(alice.core, conversation), [">before", "<sent before the change"]);
});

test("hard stop: an unapproved new device is a block too, and the core's own refusal (trust_hard_stop) is mapped to one", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "before" });
  // A second root-looking device appears for bob: no approval chain.
  world.server.devices.push({ id: uuid(), user_id: bob.userId, generation: 9, curve: "c9", ed: "e9", binding: "b", approved: true, approved_by: null, revoked: false, keys: [{ id: 1, kind: "fallback", consumed: false }] });
  world.now += 60_000;
  assert.equal((await alice.engine.trustFor(bob.userId)).block.reason, "unapproved_device");
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "no" }), (e) => e.code === "blocked");
  // The cached directory looked fine but the core knows better: still a block, not a retry.
  const world2 = new World();
  const pair = await world2.pair();
  const original = pair.alice.core.encrypt.bind(pair.alice.core);
  pair.alice.core.encrypt = async () => { throw new FakeNativeError("trust_hard_stop"); };
  await assert.rejects(pair.alice.engine.sendDm({ conversationId: pair.conversation, peerUserId: pair.bob.userId, body: "x" }), (e) => e.code === "blocked");
  assert.equal(world2.server.count("send"), 0);
  void original;
});

test("receive: legs are decrypted oldest first, a re-sync decrypts nothing twice, and late older rows are still picked up", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  for (const body of ["one", "two", "three"]) await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body });
  const first = await bob.engine.syncConversation(conversation, alice.userId);
  assert.equal(first.received, 3);
  const order = bob.core.calls.filter((c) => c[0] === "decrypt").map((c) => c[1]);
  assert.deepEqual(order, world.server.messages.map((m) => m.id), "server order");
  assert.deepEqual(bodies(bob.core, conversation), ["<one", "<two", "<three"]);
  bob.core.calls.length = 0;
  const second = await bob.engine.syncConversation(conversation, alice.userId);
  assert.equal(second.received, 0);
  assert.equal(bob.core.count("decrypt"), 0, "known messages are not even handed to the core again");
  // A row that committed late carries an OLDER timestamp than ones already seen.
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "four" });
  const late = world.server.messages.pop();
  late.created_at = "2026-10-08T00:00:00.000001Z";
  world.server.messages.push(late);
  const third = await bob.engine.syncConversation(conversation, alice.userId);
  assert.equal(third.received, 1);
  assert.ok(bodies(bob.core, conversation).includes("<four"));
});

test("receive: a prekey message triggers a key check; an undecryptable leg becomes a placeholder once; a storage fault stops the sync without one", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "first message is a prekey message" });
  const countsBefore = world.server.count("count");
  await bob.engine.syncConversation(conversation, alice.userId);
  await flush();
  assert.ok(world.server.count("count") > countsBefore, "olm_type 0 makes bob look at his key stock");
  // A leg the server hands over that cannot be read.
  const bad = { id: uuid(), conversation, sender: world.server.devices.find((d) => d.user_id === alice.userId).id, client: uuid(),
    legs: new Map([[bob.engine.getState().deviceId, { olm_type: 1, ciphertext_b64: "!!not base64 json!!" }]]), created_at: "2026-10-08T00:00:59.000000Z" };
  world.server.messages.push(bad);
  const result = await bob.engine.syncConversation(conversation, alice.userId);
  assert.deepEqual({ r: result.received, u: result.undecryptable }, { r: 0, u: 1 });
  let thread = await bob.engine.loadThread(conversation);
  assert.equal(thread.undecryptable.length, 1);
  assert.equal(thread.undecryptable[0].code, "decrypt_failed");
  bob.core.calls.length = 0;
  assert.equal((await bob.engine.syncConversation(conversation, alice.userId)).undecryptable, 0, "not retried every sync");
  assert.equal(bob.core.count("decrypt"), 0);
  const view = buildThread({ ...thread, myUserId: bob.userId });
  assert.equal(view.filter((i) => i.kind === "undecryptable").length, 1);
  assert.ok(view.every((i) => i.kind !== "undecryptable" || !("body" in i)), "no ciphertext or text on the placeholder");
  // A storage fault is about the device, not the message: no placeholder, and the next sync tries again.
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "after the fault" });
  bob.core.fault = "storage_failed";
  await assert.rejects(bob.engine.syncConversation(conversation, alice.userId), (e) => e.code === "storage_failed");
  thread = await bob.engine.loadThread(conversation);
  assert.equal(thread.undecryptable.length, 1);
  assert.equal((await bob.engine.syncConversation(conversation, alice.userId)).received, 1);
});

test("receive: messages from the account's other phone are read too, and drawn as mine", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  const alice2 = world.phone(alice.userId);
  await alice2.engine.start(alice.userId);
  world.server.approve(alice2.engine.getState().deviceId, alice.engine.getState().deviceId);
  await alice2.engine.recheck();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "from my first phone" });
  await alice2.engine.syncConversation(conversation, bob.userId);
  const thread = await alice2.engine.loadThread(conversation);
  const items = buildThread({ ...thread, myUserId: alice.userId });
  assert.deepEqual(items.map((i) => [i.body, i.mine]), [["from my first phone", true]]);
});

test("thread view: order, mine versus theirs, delivery states, and where placeholders sit", async () => {
  const message = (seq, direction, sender, body, client) => ({ seq, direction, messageId: direction === "incoming" ? `m${seq}` : null, conversationId: "c", clientMessageId: client ?? `cm${seq}`,
    senderUserId: sender, senderDeviceId: "d", kind: "text", body, senderTrust: "trusted", storedAtMs: seq });
  const items = buildThread({
    messages: [message(4, "incoming", "peer", "d"), message(3, "outgoing", "me", "c", "cm3"), message(1, "incoming", "peer", "a"), message(2, "outgoing", "me", "b", "cm2")],
    hasOlder: false,
    outbox: [{ clientMessageId: "cm3", status: "failed" }, { clientMessageId: "cm2", status: "pending" }],
    undecryptable: [{ messageId: "u1", createdAt: "2026-10-08T00:00:01.000000Z", anchorSeq: 2, code: "decrypt_failed", senderDeviceId: "d" }],
    myUserId: "me",
  });
  assert.deepEqual(items.map((i) => (i.kind === "message" ? `${i.mine ? "me" : "peer"}:${i.body}:${i.delivery}` : "x")), ["peer:a:sent", "me:b:sending", "x", "me:c:failed", "peer:d:sent"]);
  const older = buildThread({ messages: [message(10, "incoming", "peer", "z")], hasOlder: true, outbox: [], myUserId: "me",
    undecryptable: [{ messageId: "u2", createdAt: "2026-10-08T00:00:01.000000Z", anchorSeq: 3, code: "x", senderDeviceId: "d" }] });
  assert.equal(older.length, 1, "a placeholder anchored in unloaded history waits for its page");
});

test("device_revoked on send: typed, the state says why, nothing is claimed sent", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "hi" });
  world.server.devices.find((d) => d.user_id === alice.userId).revoked = true;
  world.now += 60_000;
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "after revoke" }), (e) => e instanceof E2eeSendError && e.code === "device_unusable");
  assert.equal(world.server.messages.length, 1);
});

test("unavailable build: the engine reports it and touches no storage and no network", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  phone.available = false;
  const state = await phone.engine.start(phone.userId);
  assert.equal(state.status, "unavailable");
  assert.equal(phone.core.calls.length, 0);
  assert.equal(world.server.calls.length, 0);
  assert.equal(phone.secure.map.size + phone.plain.map.size, 0);
  await assert.rejects(phone.engine.sendDm({ conversationId: uuid(), peerUserId: uuid(), body: "x" }), (e) => e.code === "not_ready");
});

test("sign-out locks the store and wipes nothing; signing back in reuses it", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  await phone.engine.start(phone.userId);
  await flush();
  await phone.engine.stop();
  assert.equal(phone.engine.getState().status, "inactive");
  assert.equal(phone.core.count("lock"), 1);
  assert.equal(phone.core.count("wipe"), 0);
  assert.equal(phone.core.open.size, 0);
  const again = await phone.engine.start(phone.userId);
  assert.equal(again.status, "ready");
  assert.equal(world.server.count("register"), 1, "same device, not a new one");
});

test("account switch: the first user's store is locked before the second user's is opened", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  await phone.engine.start(phone.userId);
  const other = uuid();
  await phone.engine.start(other);
  assert.equal(phone.engine.getState().userId, other);
  assert.equal(phone.core.open.size, 1, "exactly one store open");
});

test("a body that is empty or too long never reaches the core", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  for (const body of ["", "   ", "x".repeat(10_001)]) {
    await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body }), (e) => e.code === "invalid_body");
  }
  assert.equal(alice.core.count("encrypt"), 0);
  assert.equal((await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "x".repeat(10_000) })).kind, "sent");
});

test("a claim that finds a device gone refreshes the directory and goes round once more", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  const bob2 = world.phone(bob.userId);
  await bob2.engine.start(bob.userId);
  world.server.approve(bob2.engine.getState().deviceId, bob.engine.getState().deviceId);
  world.now += 60_000;
  await alice.engine.trustFor(bob.userId); // alice caches bob's two phones
  world.server.devices.find((d) => d.id === bob2.engine.getState().deviceId).revoked = true;
  world.server.calls.length = 0;
  const outcome = await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "hello" });
  assert.equal(outcome.kind, "sent");
  assert.deepEqual(world.server.calls.filter((c) => c[0] === "claim").map((c) => c[1].length), [2, 1], "refused whole, then only the live device");
  assert.equal(world.server.count("send"), 1);
  assert.equal(world.server.messages[0].legs.size, 1);
});

test("a live message hint syncs a DM; a group hint is ignored; a hint before the phone is ready is a no-op", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "ping" });
  await bob.engine.syncFromHint(conversation);
  assert.deepEqual(bodies(bob.core, conversation), ["<ping"]);
  const groupId = uuid();
  bob.engine.ports.conversations = { getPeer: async () => ({ kind: "group", peerUserId: null }) };
  bob.core.calls.length = 0;
  await bob.engine.syncFromHint(groupId);
  assert.equal(bob.core.calls.length, 0);
  await bob.engine.stop();
  await bob.engine.syncFromHint(conversation); // not ready: nothing happens, nothing throws
});

test("a start the server cannot be reached for is an error that recheck() recovers from, not a stuck screen", async () => {
  const world = new World();
  const userId = uuid();
  const first = world.phone(userId);
  await first.engine.start(userId);
  const second = world.phone(userId);
  await second.engine.start(userId); // pending, approved=false remembered
  second.core.kill();
  const restarted = second.newEngine();
  world.server.down = true;
  const state = await restarted.start(userId);
  assert.equal(state.status, "error");
  assert.equal(state.reason, "network");
  assert.equal(second.core.open.size, 0, "the failed start did not leave a store open");
  world.server.down = false;
  assert.equal((await restarted.recheck()).status, "pending_approval");
});

test("an approved phone that starts offline is ready from what it remembers and refuses new sends honestly", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "history" });
  world.server.down = true;
  const state = await alice.restart();
  assert.equal(state.status, "ready");
  const thread = await alice.engine.loadThread(conversation);
  assert.deepEqual(thread.messages.map((m) => m.body), ["history"], "history renders from the local store with no network");
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "nope" }), (e) => e.code === "offline");
});

test("a server without the v2 API: 'unavailable / server_not_ready', and nothing is created, registered, wiped or saved", async () => {
  const world = new World();
  world.server.noV2 = true;
  const phone = world.phone(uuid());
  const state = await phone.engine.start(phone.userId);
  assert.equal(state.status, "unavailable");
  assert.equal(state.reason, "server_not_ready");
  assert.equal(phone.core.calls.length, 0, "the store was never opened, so nothing could be created or wiped");
  assert.equal(world.server.count("register"), 0, "no registration was attempted");
  assert.equal(world.server.count("directory"), 1, "exactly one probe");
  assert.equal(phone.secure.map.size + phone.plain.map.size, 0, "nothing was remembered");
  assert.deepEqual(phone.reports, [], "no diagnostics line: it is expected, not a failure");
  // It is not an error: sends refuse as 'not ready' and no start is retried by merely asking again.
  await assert.rejects(phone.engine.sendDm({ conversationId: uuid(), peerUserId: uuid(), body: "x" }), (e) => e.code === "not_ready");
  world.server.calls.length = 0;
  assert.equal((await phone.engine.start(phone.userId)).reason, "server_not_ready");
  assert.equal(world.server.calls.length, 0, "start() again does not probe again");
});

test("a server without v2 is looked at again only on a foreground, once per session; a reconnect or a manual retry never loops", async () => {
  const world = new World();
  world.server.noV2 = true;
  const phone = world.phone(uuid());
  await phone.engine.start(phone.userId);
  world.server.calls.length = 0;
  await phone.engine.recheck("reconnect");
  await phone.engine.recheck("manual");
  await phone.engine.recheck();
  assert.equal(world.server.calls.length, 0, "neither a socket reconnect nor a manual retry re-probes");
  await phone.engine.recheck("foreground");
  assert.equal(world.server.count("directory"), 1, "one probe on the first foreground");
  assert.equal(phone.engine.getState().reason, "server_not_ready");
  await phone.engine.recheck("foreground");
  await phone.engine.recheck("foreground");
  assert.equal(world.server.count("directory"), 1, "and no more after that, however often the app is foregrounded");
  assert.equal(phone.core.calls.length, 0, "still nothing touched");
  // Signing out and back in (or the next app start) is a new session with its own budget.
  await phone.engine.stop();
  await phone.engine.start(phone.userId);
  assert.equal(world.server.count("directory"), 2);
  await phone.engine.recheck("foreground");
  assert.equal(world.server.count("directory"), 3);
});

test("once the server has v2, the one foreground re-check brings the phone up", async () => {
  const world = new World();
  world.server.noV2 = true;
  const phone = world.phone(uuid());
  await phone.engine.start(phone.userId);
  world.server.noV2 = false; // the backend is redeployed while the app is open
  const states = [];
  phone.engine.subscribe((event) => { if (event.type === "state") states.push(phone.engine.getState().status); });
  const state = await phone.engine.recheck("foreground");
  assert.equal(state.status, "ready");
  assert.equal(world.server.count("register"), 1);
  assert.ok(!states.includes("starting"), "the screen never flashed 'getting ready' while it re-asked");
});

test("only a missing API is 'coming soon': a busy server, no network, or another refusal at start stays an error", async () => {
  for (const [make, reason] of [
    [() => apiError(503, "service_unavailable"), "network"],
    [() => apiError(429, "e2ee_directory_rate_limited"), "network"],
    [() => networkError(), "network"],
    [() => apiError(403, "recipient_not_reachable"), "recipient_not_reachable"],
  ]) {
    const world = new World();
    const phone = world.phone(uuid());
    const api = phone.engine.ports.api;
    phone.engine.ports.api = { ...api, getUserDirectoryV2: async () => { throw make(); } };
    const state = await phone.engine.start(phone.userId);
    assert.equal(state.status, "error", String(reason));
    assert.equal(state.reason, reason);
    assert.equal(phone.core.calls.length, 0, "a failed probe opens nothing either");
  }
  // 405 is the same answer as 404.
  const world = new World();
  const phone = world.phone(uuid());
  const api = phone.engine.ports.api;
  phone.engine.ports.api = { ...api, getUserDirectoryV2: async () => { throw apiError(405, "Method Not Allowed"); } };
  assert.equal((await phone.engine.start(phone.userId)).reason, "server_not_ready");
});

test("the server loses the v2 routes between the probe and the registration: still 'coming soon', and nothing is wiped", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  const api = phone.engine.ports.api;
  phone.engine.ports.api = { ...api, registerDeviceV2: async () => { throw apiError(404, "Not Found"); } };
  const state = await phone.engine.start(phone.userId);
  assert.equal(state.status, "unavailable");
  assert.equal(state.reason, "server_not_ready");
  assert.equal(phone.core.count("wipe"), 0);
  assert.deepEqual(phone.reports, []);
});

test("a phone that is already registered does not probe: it starts from what it remembers even if the server has no v2 right now", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "history" });
  world.server.noV2 = true;
  world.server.calls.length = 0;
  const state = await alice.restart();
  assert.equal(state.status, "ready");
  assert.equal(world.server.count("directory"), 0, "no probe for a confirmed device");
  assert.deepEqual((await alice.engine.loadThread(conversation)).messages.map((m) => m.body), ["history"]);
});

const deleting = () => apiError(423, "account_deletion_in_progress");

test("c436: registration refused with 423 account_deletion_in_progress is 'unavailable', not an error, and is never retried", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  let attempts = 0;
  const api = phone.engine.ports.api;
  phone.engine.ports.api = { ...api, registerDeviceV2: async () => { attempts++; throw deleting(); } };
  const state = await phone.engine.start(phone.userId);
  assert.equal(state.status, "unavailable");
  assert.equal(state.reason, "account_deletion_in_progress");
  assert.equal(attempts, 1);
  assert.deepEqual(phone.reports, [], "an expected answer, not a diagnostics line");
  world.server.calls.length = 0;
  await phone.engine.start(phone.userId);
  for (const source of ["foreground", "reconnect", "manual"]) await phone.engine.recheck(source);
  assert.equal(attempts, 1, "nothing asks again, whatever the source");
  assert.equal(world.server.calls.length, 0);
  await assert.rejects(phone.engine.sendDm({ conversationId: uuid(), peerUserId: uuid(), body: "x" }), (e) => e.code === "not_ready");
});

test("c436: a send refused with 423 before anything was encrypted is a typed account_deletion: nothing stored, nothing queued, no retry", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  const api = alice.engine.ports.api;
  alice.engine.ports.api = { ...api, claimKeysV2: async () => { throw deleting(); } };
  world.server.calls.length = 0;
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "hi" }), (e) => e instanceof E2eeSendError && e.code === "account_deletion");
  assert.equal(world.server.count("send"), 0);
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.deepEqual(bodies(alice.core, conversation), []);
});

test("c436: a message that exists and is then refused with 423 is marked failed, the replay loop leaves it alone, and only the user's own retry tries again", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "warm up" });
  const api = alice.engine.ports.api;
  let posts = 0;
  alice.engine.ports.api = { ...api, sendMessageV2: async () => { posts++; throw deleting(); } };
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "refused" }), (e) => e.code === "account_deletion");
  let thread = await alice.engine.loadThread(conversation);
  assert.equal(thread.outbox.length, 1);
  assert.deepEqual([thread.outbox[0].status, thread.outbox[0].failure], ["failed", "account_deletion"]);
  assert.equal(buildThread({ ...thread, myUserId: alice.userId }).at(-1).delivery, "failed");
  await alice.engine.replayOutbox();
  await alice.engine.replayOutbox();
  assert.equal(posts, 1, "replays skip a failed entry: no retry storm against a locked account");
  alice.engine.ports.api = api; // the deletion is over (or it was the other account and that is resolved)
  assert.equal((await alice.engine.retrySend(thread.outbox[0].clientMessageId)).kind, "sent");
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
});

test("c436: 503 account_write_admission_unavailable is 'busy': refused before encryption, queued after it, and goes out on a later replay", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "warm up" });
  const busy = () => apiError(503, "account_write_admission_unavailable");
  const api = alice.engine.ports.api;
  // Before anything is encrypted (a new device needs a key claim, which the server cannot admit).
  const bob2 = world.phone(bob.userId);
  await bob2.engine.start(bob.userId);
  world.server.approve(bob2.engine.getState().deviceId, bob.engine.getState().deviceId);
  world.now += 60_000;
  alice.engine.ports.api = { ...api, claimKeysV2: async () => { throw busy(); } };
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "too early" }), (e) => e.code === "server_busy");
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.deepEqual(bodies(alice.core, conversation), [">warm up"]);
  // After encryption (the post is what the server cannot admit): the message waits.
  alice.engine.ports.api = api;
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "claimed ok" }); // establishes the session with bob's new phone
  alice.engine.ports.api = { ...api, sendMessageV2: async () => { throw busy(); } };
  assert.equal((await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "waits" })).kind, "queued");
  alice.engine.ports.api = api;
  await alice.engine.replayOutbox();
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  await bob.engine.syncConversation(conversation, alice.userId);
  assert.ok(bodies(bob.core, conversation).includes("<waits"));
});

test("c436: 403 not_a_member on a send is 'not allowed' and the message is marked failed, not retried", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "warm up" });
  const api = alice.engine.ports.api;
  alice.engine.ports.api = { ...api, sendMessageV2: async () => { throw apiError(403, "not_a_member"); } };
  await assert.rejects(alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "left" }), (e) => e.code === "not_allowed");
  assert.equal((await alice.engine.loadThread(conversation)).outbox[0].status, "failed");
});

test("c436: key top-up refused with 423 is absorbed (the phone stays ready) and logged as a fixed code only", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  await phone.engine.start(phone.userId);
  await flush();
  world.server.devices[0].keys.filter((k) => k.kind === "one_time").forEach((k) => { k.consumed = true; });
  const api = phone.engine.ports.api;
  phone.engine.ports.api = { ...api, uploadKeysV2: async () => { throw deleting(); } };
  phone.reports.length = 0;
  await phone.engine.maintainKeys().catch((e) => phone.engine.report("maintainKeys", e));
  assert.equal(phone.engine.getState().status, "ready");
  // A server-supplied detail that is not a fixed code (it could be anything) never reaches diagnostics.
  phone.engine.ports.api = { ...api, getKeyCountV2: async () => { throw apiError(500, "Hello this is plaintext! canary"); } };
  phone.engine.report("probe", apiError(500, "Hello this is plaintext! canary"));
  assert.ok(phone.reports.every(([where, code]) => /^[a-z0-9_]+$/.test(where) && /^[a-z0-9_]+$/.test(code)), JSON.stringify(phone.reports));
  assert.ok(!JSON.stringify(phone.reports).includes("plaintext"));
});

test("one install registers one device: unconfirmed registrations are capped, and the cap stops the next start without asking the server", async () => {
  const world = new World();
  const phone = world.phone(uuid());
  world.server.registerFails = true; // every reply is lost
  const states = [];
  states.push((await phone.engine.start(phone.userId)).reason); // attempt 1
  states.push((await phone.restart()).reason); // recovery: nothing registered, so wipe and attempt 2
  states.push((await phone.restart()).reason); // attempt 3
  assert.deepEqual(states, ["network", "network", "network"]);
  assert.equal(world.server.count("register"), 3);
  const record = await loadDeviceRecord(phone.secure, phone.userId);
  assert.equal(record.attempts, 3);
  const wipesBefore = phone.core.count("wipe");
  const unlocksBefore = phone.core.count("unlock");
  const stopped = await phone.restart();
  assert.equal(stopped.status, "error");
  assert.equal(stopped.reason, "registration_unconfirmed");
  assert.equal(phone.core.count("wipe"), wipesBefore, "the capped start wipes nothing");
  assert.equal(phone.core.count("unlock"), unlocksBefore, "and does not even open the store");
  assert.equal(world.server.count("register"), 3, "no fourth registration");
  assert.equal(phone.core.calls.filter((c) => c[0] === "createDevice").length, 3, "and no fourth device made locally");
  // recheck() on that error does not loop either.
  await phone.engine.recheck();
  assert.equal(world.server.count("register"), 3);
});

test("the cap also bounds the stale_generation retry: two attempts already spent leave room for exactly one more request", async () => {
  const world = new World();
  const userId = uuid();
  // The account already has a device at generation 5, so a registration at generation 2 is refused as stale.
  world.server.devices.push({ id: uuid(), user_id: userId, generation: 5, curve: "c5", ed: "e5", binding: "b", approved: true, approved_by: null, revoked: false, keys: [] });
  const phone = world.phone(userId);
  await phone.secure.set(`chirp.e2ee.device.${userId}`, JSON.stringify({ generation: 1, deviceId: null, approved: false, attempts: 2 }));
  const registersBefore = world.server.count("register");
  const state = await phone.engine.start(userId);
  assert.equal(state.status, "error");
  assert.equal(state.reason, "registration_unconfirmed");
  assert.equal(world.server.count("register") - registersBefore, 1, "one request, refused as stale; no retry beyond the cap");
});

test("a normal first sign-in leaves no attempt count behind, and a reinstall starts a fresh count", async () => {
  const world = new World();
  const userId = uuid();
  const phone = world.phone(userId);
  await phone.engine.start(userId);
  assert.equal((await loadDeviceRecord(phone.secure, userId)).attempts, undefined);
  phone.core = new FakeCore(); // reinstall
  const again = phone.newEngine();
  assert.equal((await again.start(userId)).status, "pending_approval");
  assert.equal((await loadDeviceRecord(phone.secure, userId)).attempts, undefined);
});

test("forgetConversation clears this app's own ids for one conversation (the hook for the c452 local purge) and leaves the rest", async () => {
  const world = new World();
  const { alice, bob, conversation } = await world.pair();
  const other = world.server.conversation(alice.userId, bob.userId);
  world.server.down = true;
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "one" }).catch(() => undefined);
  // (first sends need the network for a key; make sure there is something queued by warming first)
  world.server.down = false;
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "warm" });
  await alice.engine.sendDm({ conversationId: other, peerUserId: bob.userId, body: "elsewhere" });
  world.server.down = true;
  await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "queued here" });
  await alice.engine.sendDm({ conversationId: other, peerUserId: bob.userId, body: "queued there" });
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 1);
  await alice.engine.forgetConversation(conversation);
  assert.equal((await alice.engine.loadThread(conversation)).outbox.length, 0);
  assert.equal((await alice.engine.loadThread(other)).outbox.length, 1, "other conversations keep theirs");
  // The encrypted store is not this method's business: the plaintext copy is still there.
  assert.ok(bodies(alice.core, conversation).includes(">queued here"));
  // And a replay no longer tries to send the forgotten one.
  world.server.down = false;
  world.server.calls.length = 0;
  await alice.engine.replayOutbox();
  assert.equal(world.server.messages.filter((m) => m.conversation === conversation).length, 1, "only the warm-up message exists there");
});

test("error classification: no answer and busy servers are 'try later'; real answers are not", () => {
  assert.equal(isRetryLater(networkError()), true);
  assert.equal(isRetryLater(apiError(503, "x")), true);
  assert.equal(isRetryLater(apiError(429, "x")), true);
  assert.equal(isRetryLater(apiError(409, "device_list_mismatch")), false);
  assert.equal(isRetryLater(apiError(403, "recipient_not_reachable")), false);
  assert.equal(asApiError(new Error("plain")), null);
});

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

void KEY_LOW_WATER;
let failures = 0;
for (const { name, fn } of cases) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (error) {
    failures++;
    console.log(`  FAIL  ${name}\n        ${String(error?.stack ?? error).split("\n").slice(0, 6).join("\n        ")}`);
  }
}
console.log(failures === 0 ? `\nOK  verify:e2ee-dm - ${cases.length} cases` : `\n${failures} FAILURE(S) of ${cases.length}`);
process.exit(failures === 0 ? 0 : 1);
