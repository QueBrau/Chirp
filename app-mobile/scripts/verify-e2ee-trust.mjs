/**
 * Executed regressions for the trust UX logic (board c463, E2EE milestone M5).
 *
 *   npm run verify:e2ee-trust            # the cases
 *   node scripts/verify-e2ee-trust.mjs --self-test   # breaks the engine two dozen ways and proves each is caught
 *
 * The real TypeScript in src/crypto/ (engine, store, trust helpers) is transpiled and run
 * against FAKES of its two boundaries, the same way `verify:e2ee-dm` does: a stand-in for the
 * native core and an in-memory server that speaks the v2 API's real error shapes. It proves
 * the LOGIC of every trust decision: which calls are made, in which order, on which answer.
 * It does not prove the cryptography (the Rust suite) or the screens (the simulator runs).
 *
 * The fake core keeps the trust semantics of rust/src/trust.rs that the engine relies on:
 *   - the first observation pins a directory that has exactly one root and a valid chain;
 *   - a later directory that reuses a pinned device id with other keys is `identity_changed`,
 *     one with an unvouched device (a new root, a stranger) is `unapproved_device`, and neither
 *     is pinned until `acceptIdentityChange`, which re-pins exactly the last observed directory;
 *   - a device approved by a pinned, still-listed device is trusted without a prompt.
 * The fake server keeps the rules of routers/keys_v2.py: a registration is the root exactly
 * when the account has no approved, unrevoked device; a pending device is invisible to the
 * directory and visible only to its owner's pending list; revoking is idempotent.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const ROOT = fileURLToPath(new URL("../", import.meta.url));

// ---------------------------------------------------------------------------
// Loader: transpile src/crypto/*.ts to CommonJS and run it in this realm. `mutations`
// ({ "e2eeEngine.ts": [from, to] }) rewrite a file's source first; a mutation that does not
// match exactly once is an error, so a refactor cannot turn the self-test into a no-op.
// ---------------------------------------------------------------------------

function makeLoader(mutations = {}) {
  const modules = new Map();
  const applied = new Set();
  function load(path) {
    const cached = modules.get(path);
    if (cached) return cached.exports;
    let source = readFileSync(path, "utf8");
    const mutation = mutations[basename(path)];
    if (mutation) {
      const [from, to] = mutation;
      if (source.split(from).length !== 2) throw new Error(`mutation target not found exactly once in ${basename(path)}: ${from.slice(0, 80)}`);
      source = source.replace(from, to);
      applied.add(basename(path));
    }
    const output = ts.transpileModule(source, {
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
  return { src: (name) => load(resolve(ROOT, "src/crypto", `${name}.ts`)), applied };
}

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

const sha = (text) => createHash("sha256").update(text).digest("hex");
const digits = (text, n) => (BigInt(`0x${sha(text)}`) % 10n ** BigInt(n)).toString().padStart(n, "0");

/** The native core, reduced to the behaviour the trust logic relies on. One per PHONE (it is the disk). */
class FakeCore {
  constructor(trace) {
    this.trace = trace;
    this.stores = new Map();
    this.open = new Map();
    this.calls = [];
    this.counter = 0;
    this.failApprove = null;
  }
  log(name, ...args) { this.calls.push([name, ...args]); this.trace?.push(`core:${name}`); }
  count(name) { return this.calls.filter((c) => c[0] === name).length; }
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
        decrypted: new Map(), trust: new Map(), handle: null, nextKey: 1 };
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

  // ---- trust (the semantics of rust/src/trust.rs the engine depends on) ----
  evaluate(store, userId, devices) {
    const rec = store.trust.get(userId);
    const pins = rec && rec.pins.size > 0 ? rec.pins : null;
    const approvedBy = (device, known) => device.approval !== null && known.has(device.approval.approverEd25519);
    if (pins === null) {
      const roots = devices.filter((d) => d.approval === null);
      if (roots.length !== 1) return "unapproved_device";
      const known = new Set([roots[0].identityEd25519]);
      for (let progressed = true; progressed;) {
        progressed = false;
        for (const d of devices) if (!known.has(d.identityEd25519) && approvedBy(d, known)) { known.add(d.identityEd25519); progressed = true; }
      }
      return devices.every((d) => known.has(d.identityEd25519)) ? "trusted" : "unapproved_device";
    }
    if (devices.some((d) => pins.has(d.deviceId) && pins.get(d.deviceId) !== d.identityEd25519)) return "identity_changed";
    const known = new Set(devices.filter((d) => pins.get(d.deviceId) === d.identityEd25519).map((d) => d.identityEd25519));
    for (let progressed = true; progressed;) {
      progressed = false;
      for (const d of devices) if (!known.has(d.identityEd25519) && approvedBy(d, known)) { known.add(d.identityEd25519); progressed = true; }
    }
    return devices.every((d) => known.has(d.identityEd25519)) ? "trusted" : "unapproved_device";
  }
  async observeDirectory(handle, userId, devices) {
    this.log("observeDirectory", userId, devices.length);
    const store = this.store(handle);
    if (devices.length === 0) throw new FakeNativeError("invalid_input");
    const state = this.evaluate(store, userId, devices);
    const rec = store.trust.get(userId) ?? { pins: new Map(), pending: null, state };
    rec.state = state;
    if (state === "trusted") { rec.pins = new Map(devices.map((d) => [d.deviceId, d.identityEd25519])); rec.pending = null; }
    else rec.pending = devices.map((d) => ({ deviceId: d.deviceId, ed: d.identityEd25519 }));
    store.trust.set(userId, rec);
    return state;
  }
  async acceptIdentityChange(handle, userId) {
    this.log("acceptIdentityChange", userId);
    const store = this.store(handle);
    const rec = store.trust.get(userId);
    if (!rec) throw new FakeNativeError("unknown_peer");
    if (rec.state === "trusted" || !rec.pending) throw new FakeNativeError("nothing_to_accept");
    rec.pins = new Map(rec.pending.map((d) => [d.deviceId, d.ed]));
    rec.pending = null;
    rec.state = "trusted";
  }
  async approveDevice(handle, binding) {
    this.log("approveDevice", binding.identityEd25519);
    const store = this.store(handle);
    if (this.failApprove) throw new FakeNativeError(this.failApprove);
    if (binding.userId !== store.userId) throw new FakeNativeError("invalid_input");
    if (binding.identityEd25519 === store.device.ed) throw new FakeNativeError("invalid_input");
    return { approverEd25519: store.device.ed, signature: `approval:${binding.identityEd25519}:${binding.generation}` };
  }
  async safetyNumber(my, mine, peer, theirs) {
    this.log("safetyNumber");
    const half = (user, ids) => digits(`${user}|${[...ids].sort().join(",")}`, 30);
    return my < peer ? half(my, mine) + half(peer, theirs) : half(peer, theirs) + half(my, mine);
  }
  async deviceComparisonCode(ed) {
    this.log("deviceComparisonCode");
    return digits(`device-code|${ed}`, 9);
  }

  // ---- messaging (as in verify-e2ee-dm) ----
  async hasSession(handle, curve) { return this.store(handle).sessions.has(curve); }
  async encrypt(handle, conversationId, clientMessageId, body, sender, recipients) {
    this.log("encrypt", clientMessageId);
    const store = this.store(handle);
    const groups = new Map();
    for (const r of recipients) groups.set(r.device.userId, [...(groups.get(r.device.userId) ?? []), r.device]);
    groups.set(sender.userId, [...(groups.get(sender.userId) ?? []), sender]);
    for (const [userId, devices] of groups) {
      if (this.evaluate(store, userId, devices) !== "trusted") throw new FakeNativeError("trust_hard_stop");
    }
    let entry = store.outbox.get(clientMessageId);
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
      } else entry.legs.set(r.device.deviceId, { olmType: 1 });
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
    const parsed = JSON.parse(Buffer.from(leg.ciphertext, "base64").toString());
    if (parsed.to !== expected.recipientDeviceId || parsed.from !== expected.senderDeviceId) throw new FakeNativeError("envelope_mismatch");
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

/** The v2 server: the real rules and error shapes of routers/keys_v2.py and messages_v2.py. */
class FakeServer {
  constructor(trace) {
    this.trace = trace;
    this.devices = [];
    this.messages = [];
    this.down = false;
    this.calls = [];
    this.revoked = []; // ids, in the order revoked
    this.convMembers = new Map();
    this.tick = 0;
    this.registerFails = false;
    this.onRevoke = null; // (device) => void: a racing phone that appears when one is revoked
    this.beforeRegister = null; // (userId) => void: a phone approved between the last list and the registration
    this.listsOwnPending = false; // a misbehaving server that lists the caller's own device as pending
  }
  log(name, ...args) { this.calls.push([name, ...args]); this.trace?.push(`api:${name}`); }
  count(name) { return this.calls.filter((c) => c[0] === name).length; }
  conversation(a, b) { const id = uuid(); this.convMembers.set(id, [a, b]); return id; }
  live(userId) { return this.devices.filter((d) => d.user_id === userId && d.approved && !d.revoked); }
  pending(userId) { return this.devices.filter((d) => d.user_id === userId && !d.approved && !d.revoked); }
  maxGeneration(userId) { return Math.max(0, ...this.devices.filter((d) => d.user_id === userId).map((d) => d.generation)); }
  byId(id) { return this.devices.find((d) => d.id === id); }
  approve(deviceId, approverId) {
    const device = this.byId(deviceId);
    device.approved = true;
    device.approved_by = approverId;
  }
  /** A device nobody vouches for appears in an account's directory (a second root). */
  addRogueRoot(userId) {
    const device = { id: uuid(), user_id: userId, generation: this.maxGeneration(userId) + 1, curve: `rogue-curve-${idCounter}`, ed: `rogue-ed-${idCounter}`,
      binding: "b", approved: true, approved_by: null, revoked: false, keys: [{ id: 1, kind: "fallback", consumed: false }] };
    this.devices.push(device);
    return device;
  }
  apiFor(userId) {
    const guard = () => { if (this.down) throw networkError(); };
    return {
      registerDeviceV2: async (body) => {
        this.log("register", body.generation); guard();
        if (this.registerFails) throw networkError();
        this.beforeRegister?.(userId);
        const max = this.maxGeneration(userId);
        if (body.generation <= max) throw apiError(409, "stale_generation", { max_generation: max });
        const device = { id: uuid(), user_id: userId, generation: body.generation, curve: body.identity_curve25519_b64, ed: body.identity_ed25519_b64,
          binding: body.binding_signature_b64, approved: this.live(userId).length === 0, approved_by: null, revoked: false,
          keys: [...body.one_time_keys.map((k) => ({ id: k.key_id, kind: "one_time", consumed: false })), { id: body.fallback_key.key_id, kind: "fallback", consumed: false }] };
        this.devices.push(device);
        return { id: device.id, user_id: userId, approved: device.approved, generation: device.generation };
      },
      uploadKeysV2: async (deviceId, body) => {
        this.log("upload", deviceId); guard();
        const device = this.byId(deviceId);
        for (const k of body.one_time_keys) device.keys.push({ id: k.key_id, kind: "one_time", consumed: false });
        return { device_id: deviceId };
      },
      getKeyCountV2: async (deviceId) => {
        this.log("count", deviceId); guard();
        const device = this.byId(deviceId);
        return { device_id: deviceId, one_time_keys_available: device.keys.filter((k) => k.kind === "one_time" && !k.consumed).length, fallback_keys_available: 1 };
      },
      getUserDirectoryV2: async (target) => {
        this.log("directory", target); guard();
        return { user_id: target, devices: this.live(target).map((d) => {
          const approver = d.approved_by ? this.byId(d.approved_by) : null;
          return { device_id: d.id, suite: "vodozemac-olm-v1", generation: d.generation, identity_curve25519_b64: d.curve, identity_ed25519_b64: d.ed,
            binding_signature_b64: d.binding, created_at: "2026-10-08T00:00:00Z", approved_by_device_id: d.approved_by,
            approval_signature_b64: approver ? "approval" : null, approver_identity_ed25519_b64: approver ? approver.ed : null };
        }) };
      },
      listPendingDevicesV2: async () => {
        this.log("pending"); guard();
        const rows = this.pending(userId);
        if (this.listsOwnPending) rows.push(...this.live(userId).slice(0, 1));
        return { devices: rows.map((d) => ({ id: d.id, device_label: "Chirp for iOS", suite: "vodozemac-olm-v1", generation: d.generation,
          identity_curve25519_b64: d.curve, identity_ed25519_b64: d.ed, binding_signature_b64: d.binding, created_at: "2026-10-21T12:00:00Z" })) };
      },
      approveDeviceV2: async (deviceId, body) => {
        this.log("approve", deviceId, body.approver_device_id); guard();
        const target = this.byId(deviceId);
        if (!target) throw apiError(404, "device_not_found");
        if (target.user_id !== userId) throw apiError(403, "not_device_owner");
        if (target.revoked) throw apiError(403, "device_revoked");
        const approver = this.byId(body.approver_device_id);
        if (!approver || approver.user_id !== userId || approver.id === target.id || !approver.approved || approver.revoked) throw apiError(403, "invalid_approver");
        if (body.approval_signature_b64 !== `approval:${target.ed}:${target.generation}`) throw apiError(422, "invalid_approval_signature");
        if (target.approved) {
          if (target.approved_by === approver.id) return { id: target.id, approved: true };
          throw apiError(409, "device_already_approved");
        }
        target.approved = true;
        target.approved_by = approver.id;
        return { id: target.id, approved: true };
      },
      revokeDeviceV2: async (deviceId) => {
        this.log("revoke", deviceId); guard();
        const device = this.byId(deviceId);
        if (!device) throw apiError(404, "device_not_found");
        if (device.user_id !== userId) throw apiError(403, "not_device_owner");
        if (!device.revoked) { device.revoked = true; this.revoked.push(deviceId); this.onRevoke?.(device); }
        return { id: device.id, revoked_at: "2026-10-21T12:00:00Z" };
      },
      claimKeysV2: async (ids) => {
        this.log("claim", ids.slice()); guard();
        const keys = [];
        for (const id of ids) {
          const device = this.byId(id);
          if (!device || !device.approved || device.revoked) throw apiError(409, "device_unavailable", { device_ids: [id] });
          const otk = device.keys.find((k) => k.kind === "one_time" && !k.consumed);
          if (otk) otk.consumed = true;
          const used = otk ?? device.keys.find((k) => k.kind === "fallback");
          keys.push({ device_id: id, key_id: used.id, kind: used.kind, public_key_b64: "pk", signature_b64: "sig" });
        }
        return { keys };
      },
      sendMessageV2: async (conversationId, body) => {
        this.log("send", conversationId); guard();
        const sender = this.byId(body.sender_device_id);
        if (!sender || sender.revoked) throw apiError(403, "device_revoked");
        if (!sender.approved) throw apiError(403, "device_not_approved");
        const legs = new Map(body.legs.map((l) => [l.recipient_device_id, l]));
        const members = this.convMembers.get(conversationId);
        const required = members.flatMap((u) => this.live(u)).filter((d) => d.id !== sender.id).map((d) => d.id).sort();
        if (JSON.stringify([...legs.keys()].sort()) !== JSON.stringify(required)) throw apiError(409, "device_list_mismatch", { device_ids: required });
        const message = { id: uuid(), conversation: conversationId, sender: sender.id, client: body.client_message_id, legs, created_at: `2026-10-21T00:00:${String(++this.tick).padStart(2, "0")}.000000Z` };
        this.messages.push(message);
        return { id: message.id };
      },
      listMessagesV2: async (conversationId, deviceId, options = {}) => {
        this.log("history", conversationId); guard();
        const device = this.byId(deviceId);
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

// ---------------------------------------------------------------------------
// The suite. `build(m)` returns the cases for one load of the modules, so the self-test can
// run the very same cases against a mutated engine.
// ---------------------------------------------------------------------------

function build(m) {
  const { E2eeEngine } = m.src("e2eeEngine");
  const { E2eeSendError, E2eeTrustError, MAX_RESET_REVOKE_PASSES } = m.src("e2eeTypes");
  const trust = m.src("e2eeTrust");

  class Phone {
    constructor(world, userId) {
      this.world = world;
      this.userId = userId;
      this.core = new FakeCore(world.trace);
      this.secure = memoryKv();
      this.plain = memoryKv();
      this.reports = [];
      this.ownerEpoch = 0;
      this.engine = this.newEngine();
    }
    newEngine() {
      return new E2eeEngine({
        captureOwner: () => { const epoch = this.ownerEpoch; return () => epoch === this.ownerEpoch; },
        crypto: this.core,
        api: this.world.server.apiFor(this.userId),
        secure: this.secure,
        plain: this.plain,
        available: () => true,
        newId: uuid,
        now: () => this.world.now,
        report: (where, code) => this.reports.push([where, code]),
        conversations: { getPeer: async (conversationId, me) => {
          const members = this.world.server.convMembers.get(conversationId);
          return { kind: "dm", peerUserId: members.find((u) => u !== me) ?? null };
        } },
      });
    }
    /** A reinstall: the app's files are gone (new core store), the Keychain record stays. */
    reinstall() {
      this.core = new FakeCore(this.world.trace);
      this.engine = this.newEngine();
      return this.engine.start(this.userId);
    }
    get deviceId() { return this.engine.getState().deviceId; }
  }

  class World {
    constructor() { this.trace = []; this.server = new FakeServer(this.trace); this.now = 1_000_000; }
    phone(userId) { return new Phone(this, userId); }
    async pair() {
      const alice = this.phone(uuid()), bob = this.phone(uuid());
      await alice.engine.start(alice.userId);
      await bob.engine.start(bob.userId);
      const conversation = this.server.conversation(alice.userId, bob.userId);
      // A first message each way pins both directories and warms the sessions.
      await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "hello" });
      await bob.engine.syncConversation(conversation, alice.userId);
      await bob.engine.sendDm({ conversationId: conversation, peerUserId: alice.userId, body: "hi" });
      await alice.engine.syncConversation(conversation, bob.userId);
      return { alice, bob, conversation };
    }
    expire() { this.now += 60_000; }
  }

  const bodies = (core, conversation) => [...core.stores.values()].flatMap((s) => s.messages)
    .filter((x) => x.conversationId === conversation).sort((a, b) => a.seq - b.seq)
    .map((x) => `${x.direction === "outgoing" ? ">" : "<"}${x.body}`);
  const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); };
  const verifiedKey = (me, peer) => `chirp.e2ee.verified.${me}.${peer}`;

  /** Bob's account is reset: his phone is gone, a new phone becomes his root. Returns the new phone. */
  async function resetBob(world, bob) {
    for (const device of world.server.live(bob.userId)) { device.revoked = true; world.server.revoked.push(device.id); }
    const fresh = world.phone(bob.userId);
    await fresh.engine.start(bob.userId);
    assert.equal(fresh.engine.getState().status, "ready", "with nobody left to approve it, the new phone is the root");
    world.expire();
    return fresh;
  }

  const cases = [];
  const test = (name, fn) => cases.push({ name, fn });

  // ---- pure helpers ----

  test("display helpers: twelve groups of five, a code in groups of three, malformed input is never half-drawn", () => {
    const number = "0123456789".repeat(6);
    const groups = trust.groupSafetyNumber(number);
    assert.equal(groups.length, 12);
    assert.ok(groups.every((g) => /^\d{5}$/.test(g)));
    assert.equal(groups.join(""), number);
    assert.deepEqual(trust.groupSafetyNumber(number.slice(1)), []);
    assert.deepEqual(trust.groupSafetyNumber(`${number}0`), []);
    assert.deepEqual(trust.groupSafetyNumber("x".repeat(60)), []);
    assert.equal(trust.groupComparisonCode("063120697"), "063 120 697");
    assert.equal(trust.groupComparisonCode("12345"), "12345");
    assert.equal(trust.groupComparisonCode("12345678a"), "12345678a");
  });

  test("the comparison code algorithm in the committed vectors is the documented one (a third implementation)", () => {
    const vectors = JSON.parse(readFileSync(resolve(ROOT, "modules/chirp-crypto/rust/test-vectors/e2ee-v1.json"), "utf8"));
    const code = (hex) => {
      const key = Buffer.from(hex, "hex");
      const domain = Buffer.from("chirp-e2ee-v1/device-code");
      const head = Buffer.alloc(2); head.writeUInt16BE(domain.length);
      const len = Buffer.alloc(4); len.writeUInt32BE(key.length);
      const digest = createHash("sha256").update(Buffer.concat([head, domain, len, key])).digest();
      return (digest.readBigUInt64BE(0) % 1_000_000_000n).toString().padStart(9, "0");
    };
    assert.ok(vectors.device_comparison_code.cases.length >= 3);
    for (const entry of vectors.device_comparison_code.cases) assert.equal(code(entry.identity_ed25519), entry.digits);
    assert.equal(code("00".repeat(32)), "063120697", "the pinned reference value");
  });

  test("directory fingerprints ignore order and notice any device, key or generation change", () => {
    const d = (n, extra = {}) => ({ userId: "u", deviceId: `d${n}`, generation: n, identityEd25519: `ed${n}`, identityCurve25519: `cu${n}`, ...extra });
    const a = [d(1), d(2)];
    assert.equal(trust.directoryFingerprint(a), trust.directoryFingerprint([d(2), d(1)]));
    for (const other of [[d(1)], [d(1), d(2, { identityEd25519: "x" })], [d(1), d(2, { identityCurve25519: "x" })], [d(1), d(2, { generation: 9 })], [d(1), d(2), d(3)]]) {
      assert.notEqual(trust.directoryFingerprint(a), trust.directoryFingerprint(other));
    }
  });

  // ---- safety number and verified ----

  test("both phones compute the same safety number from fresh directories", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    const fromAlice = await alice.engine.safetyView(bob.userId);
    const fromBob = await bob.engine.safetyView(alice.userId);
    assert.match(fromAlice.number, /^\d{60}$/);
    assert.equal(fromAlice.number, fromBob.number);
    assert.equal(fromAlice.state, "trusted");
    assert.equal(fromAlice.ownState, "trusted");
    assert.equal(fromAlice.verified, false);
    // Fresh, not cached: the read goes to the server even though the directory is warm.
    const before = world.server.count("directory");
    await alice.engine.safetyView(bob.userId);
    assert.equal(world.server.count("directory") - before, 2, "both directories are fetched again");
  });

  test("a safety view never falls back to a stale directory when the server cannot be asked", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    world.server.down = true;
    await assert.rejects(alice.engine.safetyView(bob.userId), (e) => e instanceof E2eeTrustError && e.code === "network");
  });

  test("verified: marked against the number in force, shown by trustFor, and stored under the pair", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    const view = await alice.engine.safetyView(bob.userId);
    assert.equal(await alice.engine.setVerified(bob.userId, true, view.fingerprint), true);
    assert.equal((await alice.engine.trustFor(bob.userId)).verified, true);
    assert.equal(alice.secure.map.get(verifiedKey(alice.userId, bob.userId)), view.number);
    assert.equal((await alice.engine.safetyView(bob.userId)).verified, true);
    assert.equal(await alice.engine.setVerified(bob.userId, false, view.fingerprint), false);
    assert.equal((await alice.engine.trustFor(bob.userId)).verified, false);
    assert.equal(alice.secure.map.has(verifiedKey(alice.userId, bob.userId)), false);
  });

  test("verified resets by itself when the contact's identity changes, and when either side adds a phone", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    const view = await alice.engine.safetyView(bob.userId);
    await alice.engine.setVerified(bob.userId, true, view.fingerprint);
    assert.equal((await alice.engine.trustFor(bob.userId)).verified, true);

    // The contact adds a phone that is properly approved: no hard stop, but the number moved.
    const bob2 = world.phone(bob.userId);
    await bob2.engine.start(bob.userId);
    world.server.approve(bob2.deviceId, bob.deviceId);
    world.expire();
    const afterAdd = await alice.engine.trustFor(bob.userId);
    assert.equal(afterAdd.block, null, "an approved phone is not a hard stop");
    assert.equal(afterAdd.verified, false, "the mark belonged to the old number");
    assert.equal((await alice.engine.safetyView(bob.userId)).verified, false, "and the safety screen agrees");

    // Marking the new number works, and a reset of the contact (hard stop) clears it again.
    const view2 = await alice.engine.safetyView(bob.userId);
    await alice.engine.setVerified(bob.userId, true, view2.fingerprint);
    assert.equal((await alice.engine.trustFor(bob.userId)).verified, true);
    await resetBob(world, bob);
    const stopped = await alice.engine.trustFor(bob.userId);
    assert.equal(stopped.verified, false);
    assert.equal(stopped.block.userId, bob.userId);

    // Accepting is not verifying: the old mark does not come back for the accepted code.
    await alice.engine.acceptIdentityChange(bob.userId, stopped.fingerprint);
    const accepted = await alice.engine.trustFor(bob.userId);
    assert.equal(accepted.block, null);
    assert.equal(accepted.verified, false);
  });

  test("verified resets when THIS account's own phones change (the number covers both sides)", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    const view = await alice.engine.safetyView(bob.userId);
    await alice.engine.setVerified(bob.userId, true, view.fingerprint);
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    world.server.approve(alice2.deviceId, alice.deviceId);
    world.expire();
    assert.equal((await alice.engine.trustFor(bob.userId)).verified, false);
  });

  test("setVerified refuses when the directories moved after the person looked, and while a hard stop is open", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    const view = await alice.engine.safetyView(bob.userId);
    const bob2 = world.phone(bob.userId);
    await bob2.engine.start(bob.userId);
    world.server.approve(bob2.deviceId, bob.deviceId);
    await assert.rejects(alice.engine.setVerified(bob.userId, true, view.fingerprint), (e) => e.code === "changed_again");
    assert.equal(alice.secure.map.has(verifiedKey(alice.userId, bob.userId)), false, "nothing was stored");

    const reset = await resetBob(world, bob);
    void reset;
    const stopped = await alice.engine.safetyView(bob.userId);
    assert.notEqual(stopped.state, "trusted");
    await assert.rejects(alice.engine.setVerified(bob.userId, true, stopped.fingerprint), (e) => e.code === "blocked");
    assert.equal(alice.secure.map.has(verifiedKey(alice.userId, bob.userId)), false);
  });

  // ---- hard stop ----

  test("hard stop: sending is blocked with the reason, receiving continues", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    const bobNew = await resetBob(world, bob);
    // The new phone writes to alice (its own first contact with her is clean).
    await bobNew.engine.sendDm({ conversationId: conversation, peerUserId: alice.userId, body: "new phone here" });

    const claimsBefore = world.server.count("claim");
    await assert.rejects(
      alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "blocked?" }),
      (e) => e instanceof E2eeSendError && e.code === "blocked" && e.block.userId === bob.userId && e.block.reason === "unapproved_device",
    );
    assert.ok(!bodies(alice.core, conversation).includes(">blocked?"), "nothing was encrypted for a blocked send");
    assert.equal(world.server.messages.length, 3, "and nothing was posted");
    assert.equal(world.server.count("claim"), claimsBefore, "and no one-time key was claimed (a claim burns a key on the server)");

    const result = await alice.engine.syncConversation(conversation, bob.userId);
    assert.equal(result.received, 1, "receiving does not check trust");
    assert.ok(bodies(alice.core, conversation).includes("<new phone here"));
    const t = await alice.engine.trustFor(bob.userId);
    assert.deepEqual(t.block, { userId: bob.userId, reason: "unapproved_device" });
  });

  test("hard stop: a changed identity (same device id, other keys) is identity_changed and blocks the same way", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    world.server.byId(bob.deviceId).ed = "ed-other-key";
    world.expire();
    await assert.rejects(
      alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "x" }),
      (e) => e.code === "blocked" && e.block.reason === "identity_changed",
    );
  });

  test("accept: re-pins what was observed, asks the core again, and sending works afterwards", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    await resetBob(world, bob);
    const t = await alice.engine.trustFor(bob.userId);
    assert.ok(t.fingerprint, "the banner carries what it was about");
    alice.core.calls.length = 0;
    const state = await alice.engine.acceptIdentityChange(bob.userId, t.fingerprint);
    assert.equal(state, "trusted");
    const sequence = alice.core.calls.map((c) => c[0]).filter((n) => n === "observeDirectory" || n === "acceptIdentityChange");
    assert.deepEqual(sequence, ["observeDirectory", "acceptIdentityChange", "observeDirectory"], "observe what is current, accept it, check again");
    assert.equal((await alice.engine.trustFor(bob.userId)).block, null);
    const outcome = await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "after accept" });
    assert.equal(outcome.kind, "sent");
  });

  test("accept refuses (and the core is not asked) when the directory moved after the banner or safety screen", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    await resetBob(world, bob);
    const t = await alice.engine.trustFor(bob.userId);
    // Another phone appears for the contact between the banner and the tap.
    const another = world.phone(bob.userId);
    await another.engine.start(bob.userId);
    world.server.approve(another.deviceId, world.server.live(bob.userId)[0].id);
    alice.core.calls.length = 0;
    await assert.rejects(alice.engine.acceptIdentityChange(bob.userId, t.fingerprint), (e) => e.code === "changed_again");
    assert.equal(alice.core.count("acceptIdentityChange"), 0);
    assert.equal(alice.core.count("observeDirectory"), 0, "nothing was observed or pinned either");
    // The same, from the safety screen's fingerprint.
    const view = await alice.engine.safetyView(bob.userId);
    const third = world.phone(bob.userId);
    await third.engine.start(bob.userId);
    world.server.approve(third.deviceId, world.server.live(bob.userId)[0].id);
    await assert.rejects(alice.engine.acceptIdentityChange(bob.userId, view.peerFingerprint), (e) => e.code === "changed_again");
    // Without a binding the current directory is what is accepted.
    assert.equal(await alice.engine.acceptIdentityChange(bob.userId, null), "trusted");
  });

  test("accept is not an error when the directory went back to what is pinned in the meantime", async () => {
    const world = new World();
    const { alice, bob } = await world.pair();
    const original = world.server.live(bob.userId)[0];
    const reset = await resetBob(world, bob);
    void reset;
    const t = await alice.engine.trustFor(bob.userId);
    // The contact undoes it: the new phone is revoked and the original comes back.
    for (const d of world.server.live(bob.userId)) d.revoked = true;
    original.revoked = false;
    const state = await alice.engine.acceptIdentityChange(bob.userId, null);
    void t;
    assert.equal(state, "trusted");
  });

  test("own account: a phone nobody vouched for on YOUR account blocks sending, and accepting clears it", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    world.server.addRogueRoot(alice.userId);
    world.expire();
    const t = await alice.engine.trustFor(bob.userId);
    assert.deepEqual(t.block, { userId: alice.userId, reason: "unapproved_device" }, "the block is about the account's own phones");
    await assert.rejects(
      alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "x" }),
      (e) => e.code === "blocked" && e.block.userId === alice.userId,
    );
    const view = await alice.engine.safetyView(bob.userId);
    assert.equal(view.state, "trusted", "the contact is fine");
    assert.equal(view.ownState, "unapproved_device");
    assert.equal(await alice.engine.acceptIdentityChange(alice.userId, view.ownFingerprint), "trusted");
    assert.equal((await alice.engine.trustFor(bob.userId)).block, null);
  });

  // ---- new phone: approval ----

  test("a new phone shows the SAME code on both phones, and the approving phone lists it", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    const state = await alice2.engine.start(alice.userId);
    assert.equal(state.status, "pending_approval");
    const own = await alice2.engine.ownComparisonCode();
    assert.match(own, /^\d{9}$/);
    const pending = await alice.engine.refreshPendingDevices();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].deviceId, alice2.deviceId);
    assert.equal(pending[0].code, own, "the approving phone derives the code from the key the server lists");
    assert.equal(pending[0].identityEd25519, world.server.byId(alice2.deviceId).ed);
  });

  test("approve: this phone signs the listed binding, the server records it, then the list is read again", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    await alice.engine.refreshPendingDevices();
    world.trace.length = 0;
    await alice.engine.approvePending(alice2.deviceId);
    const order = world.trace.filter((t) => ["core:approveDevice", "api:approve", "api:pending"].includes(t));
    assert.deepEqual(order, ["core:approveDevice", "api:approve", "api:pending"]);
    assert.deepEqual(world.server.calls.find((c) => c[0] === "approve").slice(1), [alice2.deviceId, alice.deviceId], "signed as THIS phone");
    assert.equal(world.server.byId(alice2.deviceId).approved, true);
    assert.deepEqual(alice.engine.getPendingApprovals(), []);

    // The new phone notices without a restart and is ready.
    const next = await alice2.engine.recheck();
    assert.equal(next.status, "ready");
    // ...and works: it sends to bob, who does NOT hard-stop for an approved device.
    await alice2.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "from my new phone" });
    world.expire();
    const bobTrust = await bob.engine.trustFor(alice.userId);
    assert.equal(bobTrust.block, null, "an approved device is not a hard stop on the contact's side");
    await bob.engine.syncConversation(conversation, alice.userId);
    assert.ok(bodies(bob.core, conversation).includes("<from my new phone"));
    // alice's own first phone still trusts her account.
    assert.equal((await alice.engine.trustFor(bob.userId)).block, null);
  });

  test("approve: a retry after a lost reply, and a phone that was revoked meanwhile, are handled", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    await alice.engine.refreshPendingDevices();
    // The first reply is lost after the server recorded it.
    const api = alice.engine.ports.api;
    let lost = true;
    alice.engine.ports.api = { ...api, approveDeviceV2: async (...args) => { const out = await api.approveDeviceV2(...args); if (lost) { lost = false; throw networkError(); } return out; } };
    await assert.rejects(alice.engine.approvePending(alice2.deviceId), (e) => e.code === "network");
    assert.equal(world.server.byId(alice2.deviceId).approved, true);
    await alice.engine.approvePending(alice2.deviceId); // the retry: still listed locally, server answers idempotently
    assert.deepEqual(alice.engine.getPendingApprovals(), []);

    // A second pending phone that gets revoked before the tap.
    const alice3 = world.phone(alice.userId);
    await alice3.engine.start(alice.userId);
    await alice.engine.refreshPendingDevices();
    world.server.byId(alice3.deviceId).revoked = true;
    await assert.rejects(alice.engine.approvePending(alice3.deviceId), (e) => e.code === "gone");
    // Something that is not in the list at all.
    await assert.rejects(alice.engine.approvePending(uuid()), (e) => e.code === "gone");
  });

  test("approve: a phone that is not ready cannot approve, and the core refusing means nothing is sent to the server", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    // The pending phone itself cannot approve anything (and lists nothing).
    assert.deepEqual(await alice2.engine.refreshPendingDevices(), []);
    await assert.rejects(alice2.engine.approvePending(alice2.deviceId), (e) => e.code === "not_ready");
    await alice.engine.refreshPendingDevices();
    alice.core.failApprove = "invalid_signature";
    world.server.calls.length = 0;
    await assert.rejects(alice.engine.approvePending(alice2.deviceId), (e) => e.code === "rejected");
    assert.equal(world.server.count("approve"), 0);
    assert.equal(world.server.byId(alice2.deviceId).approved, false);
  });

  test("the pending list never includes this phone itself, and 'decide later' hides a phone until the foreground", async () => {
    const world = new World();
    const { alice } = await world.pair();
    world.server.listsOwnPending = true;
    assert.deepEqual(await alice.engine.refreshPendingDevices(), [], "a row naming this phone is dropped");
    world.server.listsOwnPending = false;
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    assert.equal((await alice.engine.refreshPendingDevices()).length, 1);
    alice.engine.snoozePending(alice2.deviceId);
    assert.equal(alice.engine.getPendingApprovals().length, 0);
    assert.equal((await alice.engine.refreshPendingDevices()).length, 0, "a poll does not bring it back");
    assert.equal((await alice.engine.refreshPendingDevices({ foreground: true })).length, 1, "returning to the foreground does");
  });

  test("a failed poll keeps the list, and a signed-out engine clears it", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    await alice.engine.refreshPendingDevices();
    world.server.down = true;
    assert.equal((await alice.engine.refreshPendingDevices()).length, 1);
    assert.ok(alice.reports.some(([where]) => where === "pending_devices"));
    world.server.down = false;
    await alice.engine.stop();
    assert.deepEqual(alice.engine.getPendingApprovals(), []);
  });

  test("a phone removed from the account stops offering approvals and never pretends to be ready again", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    assert.equal((await alice.engine.refreshPendingDevices()).length, 1);
    // Another phone's Reset security removed this one.
    world.server.byId(alice.deviceId).revoked = true;
    await assert.rejects(alice.engine.syncConversation(conversation, bob.userId));
    assert.equal(alice.engine.getState().status, "error");
    assert.equal(alice.engine.getState().reason, "device_revoked");
    assert.deepEqual(alice.engine.getPendingApprovals(), [], "a prompt for another phone must not outlive this phone's readiness");
    await assert.rejects(alice.engine.approvePending(alice2.deviceId), (e) => e.code === "not_ready");
    // Asking again (foreground, retry) does not restart it: a revoked device is final.
    const unlocks = alice.core.count("unlock");
    assert.equal((await alice.engine.recheck("foreground")).reason, "device_revoked");
    assert.equal((await alice.engine.recheck("manual")).status, "error");
    assert.equal(alice.core.count("unlock"), unlocks, "no restart was attempted");
  });

  test("Not me: the pending phone is revoked, the list empties, and an already-gone phone counts as done", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    await alice.engine.refreshPendingDevices();
    await alice.engine.denyPending(alice2.deviceId);
    assert.deepEqual(world.server.revoked, [alice2.deviceId]);
    assert.deepEqual(alice.engine.getPendingApprovals(), []);
    // The revoked phone never becomes ready.
    assert.equal((await alice2.engine.recheck()).status, "pending_approval");
    // Again (the phone vanished on the server): no error.
    await alice.engine.denyPending(alice2.deviceId);
    await alice.engine.denyPending(uuid()); // a 404
    // A network failure is reported as such and changes nothing.
    const alice3 = world.phone(alice.userId);
    await alice3.engine.start(alice.userId);
    await alice.engine.refreshPendingDevices();
    world.server.down = true;
    await assert.rejects(alice.engine.denyPending(alice3.deviceId), (e) => e.code === "network");
    world.server.down = false;
    assert.equal(world.server.byId(alice3.deviceId).revoked, false);
  });

  // ---- Reset security ----

  test("Reset security (a lost phone / a reinstall): revokes only OTHER phones, registers once as the root, and the contact is hard-stopped", async () => {
    const world = new World();
    const { alice, bob, conversation } = await world.pair();
    const oldRoot = alice.deviceId;
    // A reinstall: the server still lists the old install as approved, so this phone is pending.
    const state = await alice.reinstall();
    assert.equal(state.status, "pending_approval");
    const pendingId = alice.deviceId;
    assert.notEqual(pendingId, oldRoot);
    const registersBefore = world.server.count("register");
    const wipesBefore = alice.core.calls.filter((c) => c[0] === "wipe").length;
    world.trace.length = 0;

    await alice.engine.resetSecurity();

    const after = alice.engine.getState();
    assert.equal(after.status, "ready");
    assert.notEqual(after.deviceId, oldRoot);
    assert.notEqual(after.deviceId, pendingId, "a fresh device, not the pending one");
    assert.equal(world.server.byId(after.deviceId).approved, true, "it is the root");
    assert.equal(world.server.byId(after.deviceId).approved_by, null);
    assert.equal(world.server.count("register") - registersBefore, 1, "exactly one registration");
    // Never the current device: the old root and the abandoned pending registration only.
    assert.deepEqual([...world.server.revoked].sort(), [oldRoot, pendingId].sort());
    assert.equal(world.server.byId(after.deviceId).revoked, false);
    // Every other phone was revoked BEFORE the new registration (that is what makes it a root).
    const firstRegister = world.trace.indexOf("api:register");
    assert.ok(firstRegister > world.trace.indexOf("api:revoke"), "others first, then register");
    assert.equal(world.trace.slice(0, firstRegister).filter((t) => t === "api:revoke").length, 1, "the old root is revoked before registering");
    // The pending store is wiped, nothing else.
    assert.equal(alice.core.calls.filter((c) => c[0] === "wipe").length - wipesBefore, 1);
    // Persisted: the next start finds a ready phone.
    const record = JSON.parse(alice.secure.map.get(`chirp.e2ee.device.${alice.userId}`));
    assert.deepEqual({ deviceId: record.deviceId, approved: record.approved, generation: record.generation }, { deviceId: after.deviceId, approved: true, generation: after.generation });

    // The contact sees the hard stop; the receiving side keeps working.
    world.expire();
    const t = await bob.engine.trustFor(alice.userId);
    assert.equal(t.block?.userId, alice.userId);
    assert.equal(t.block?.reason, "unapproved_device");
    await assert.rejects(bob.engine.sendDm({ conversationId: conversation, peerUserId: alice.userId, body: "x" }), (e) => e.code === "blocked");
    // The reset phone can talk (first contact with bob is clean) and bob can still read it.
    await alice.engine.sendDm({ conversationId: conversation, peerUserId: bob.userId, body: "reset done" });
    await bob.engine.syncConversation(conversation, alice.userId);
    assert.ok(bodies(bob.core, conversation).includes("<reset done"));
  });

  test("Reset security: other pending phones are revoked too, and a phone is not reset when not waiting for approval", async () => {
    const world = new World();
    const { alice } = await world.pair();
    await assert.rejects(alice.engine.resetSecurity(), (e) => e.code === "not_pending", "a ready phone has nothing to reset");
    const second = world.phone(alice.userId);
    await second.engine.start(alice.userId);
    const third = world.phone(alice.userId);
    await third.engine.start(alice.userId);
    await third.engine.resetSecurity();
    assert.equal(third.engine.getState().status, "ready");
    assert.ok(world.server.revoked.includes(alice.deviceId));
    assert.ok(world.server.revoked.includes(second.deviceId), "a pending phone of the account is removed as well");
    assert.equal(world.server.live(alice.userId).length, 1);
    assert.equal(world.server.live(alice.userId)[0].id, third.deviceId);
  });

  test("Reset security cannot loop: a racing phone that reappears stops it after the revoke passes, with nothing wiped", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const fresh = world.phone(alice.userId);
    await fresh.engine.start(alice.userId);
    const registersBefore = world.server.count("register");
    const wipesBefore = fresh.core.calls.filter((c) => c[0] === "wipe").length;
    let spawned = 0;
    world.server.onRevoke = () => { if (spawned < 40) { spawned++; world.server.addRogueRoot(alice.userId); } };
    await assert.rejects(fresh.engine.resetSecurity(), (e) => e.code === "reset_incomplete");
    assert.ok(world.server.revoked.length <= MAX_RESET_REVOKE_PASSES + 1, `at most ${MAX_RESET_REVOKE_PASSES} passes of revoking: ${world.server.revoked.length}`);
    assert.equal(world.server.count("register"), registersBefore, "no registration while the others are not gone");
    assert.equal(fresh.core.calls.filter((c) => c[0] === "wipe").length, wipesBefore, "and nothing local was wiped");
    assert.equal(fresh.engine.getState().status, "pending_approval");
    assert.equal(world.server.byId(fresh.deviceId).revoked, false, "the current phone was never revoked");
  });

  test("Reset security does not retry when the server does not make the new phone the root", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const fresh = world.phone(alice.userId);
    await fresh.engine.start(alice.userId);
    const registersBefore = world.server.count("register");
    // A phone is approved on the account between the last check and the registration.
    world.server.beforeRegister = (userId) => { world.server.beforeRegister = null; const rogue = world.server.addRogueRoot(userId); rogue.generation = 1; };
    await assert.rejects(fresh.engine.resetSecurity(), (e) => e.code === "reset_incomplete");
    assert.equal(world.server.count("register") - registersBefore, 1, "one registration and no second try");
    assert.equal(fresh.engine.getState().status, "pending_approval", "honest: it is not the root");
    assert.equal(world.server.byId(fresh.deviceId).revoked, false);
  });

  test("Reset security: a network failure while revoking changes nothing local; a failure after leaves a state the next start repairs as the root", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const oldRoot = alice.deviceId;
    await alice.reinstall();
    const pendingId = alice.deviceId;
    const wipes = () => alice.core.calls.filter((c) => c[0] === "wipe").length;
    const wipesBefore = wipes();
    // 1. The network drops while revoking.
    const api = alice.engine.ports.api;
    alice.engine.ports.api = { ...api, revokeDeviceV2: async () => { throw networkError(); } };
    await assert.rejects(alice.engine.resetSecurity(), (e) => e.code === "network");
    assert.equal(wipes(), wipesBefore);
    assert.equal(alice.engine.getState().status, "pending_approval");
    assert.equal(alice.deviceId, pendingId);
    assert.equal(world.server.byId(oldRoot).revoked, false);

    // 2. Revoking works, then registering fails: the other phones are gone, this one is unfinished.
    alice.engine.ports.api = { ...api, registerDeviceV2: async () => { throw networkError(); } };
    await assert.rejects(alice.engine.resetSecurity(), (e) => e.code === "network");
    assert.equal(world.server.byId(oldRoot).revoked, true);
    assert.equal(alice.engine.getState().status, "error");
    assert.equal(alice.engine.getState().reason, "network");
    // The ordinary retry (a recheck of an error state) finishes it, as the root.
    alice.engine.ports.api = api;
    const next = await alice.engine.recheck("manual");
    assert.equal(next.status, "ready", `reason: ${next.reason}`);
    assert.equal(world.server.byId(next.deviceId).approved_by, null);
    assert.equal(world.server.live(alice.userId).length, 1);
  });

  test("Reset security: a second tap joins the run in progress instead of starting another", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const fresh = world.phone(alice.userId);
    await fresh.engine.start(alice.userId);
    const registersBefore = world.server.count("register");
    const first = fresh.engine.resetSecurity();
    const second = fresh.engine.resetSecurity();
    const finished = Promise.allSettled([first, second]);
    assert.equal(first, second, "the same promise");
    for (const result of await finished) {
      if (result.status === "rejected") throw result.reason;
    }
    assert.equal(world.server.count("register") - registersBefore, 1);
    assert.equal(fresh.engine.getState().status, "ready");
    // Afterwards it is ready, so a further reset is refused.
    await assert.rejects(fresh.engine.resetSecurity(), (e) => e.code === "not_pending");
  });

  test("Reset security survives a sign-out in the middle: nothing is left open", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const fresh = world.phone(alice.userId);
    await fresh.engine.start(alice.userId);
    const api = fresh.engine.ports.api;
    let signedOut = null;
    fresh.engine.ports.api = { ...api, registerDeviceV2: async (body) => { signedOut = fresh.engine.stop(); return api.registerDeviceV2(body); } };
    await assert.rejects(fresh.engine.resetSecurity());
    await signedOut;
    assert.equal(fresh.engine.getState().status, "inactive");
    const open = [...fresh.core.stores.values()].filter((store) => store.handle !== null);
    assert.equal(open.length, 0, "no store is left unlocked");
  });

  test("c452: Reset security refuses an identity switch while reading pending devices before revoking anything", async () => {
    const world = new World(); const { alice } = await world.pair();
    const fresh = world.phone(alice.userId); await fresh.engine.start(alice.userId);
    let entered, release;
    const started = new Promise(resolve => { entered = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    fresh.engine.ports.api.listPendingDevicesV2 = async () => { entered(); return gate; };
    const revokes = world.server.count("revoke"), wipes = fresh.core.count("wipe");
    const resetting = fresh.engine.resetSecurity();
    const settled = resetting.then(() => "completed", () => "rejected");
    try {
      assert.equal(await Promise.race([started.then(() => "entered"), settled]), "entered", "reset must read pending devices before changing native state");
      fresh.ownerEpoch += 1;
      release({ devices: [{ id: uuid() }] });
      await assert.rejects(resetting);
    } finally {
      release({ devices: [] });
      await settled;
    }
    assert.equal(world.server.count("revoke"), revokes);
    assert.equal(fresh.core.count("wipe"), wipes);
    await assert.rejects(fresh.engine.resetSecurity(), error => error.code === "not_pending");
  });

  test("c452: Reset security cannot revoke or become ready after its registration changes identity", async () => {
    const world = new World(); const { alice } = await world.pair();
    const fresh = world.phone(alice.userId); await fresh.engine.start(alice.userId);
    const pendingId = fresh.deviceId;
    const register = fresh.engine.ports.api.registerDeviceV2;
    fresh.engine.ports.api.registerDeviceV2 = async body => {
      const response = await register(body);
      fresh.ownerEpoch += 1;
      return response;
    };
    await assert.rejects(fresh.engine.resetSecurity());
    assert.equal(world.server.byId(pendingId).revoked, false, "the old continuation must not revoke through the new account");
    assert.notEqual(fresh.engine.getState().status, "ready");
    assert.equal([...fresh.core.stores.values()].filter(store => store.handle !== null).length, 0);
  });

  test("c452: Reset security journals its new generation before native unlock, including a crash", async () => {
    const world = new World(); const { alice } = await world.pair();
    const fresh = world.phone(alice.userId); await fresh.engine.start(alice.userId);
    const expected = fresh.engine.getState().generation + 1;
    const unlock = fresh.core.unlock.bind(fresh.core);
    fresh.core.unlock = async (userId, generation) => {
      const handle = await unlock(userId, generation);
      if (generation === expected) {
        assert.equal(JSON.parse(await fresh.secure.get(`chirp.e2ee.device.${userId}`)).generation, expected);
        throw new Error("process_died_after_unlock");
      }
      return handle;
    };
    await assert.rejects(fresh.engine.resetSecurity());
    assert.equal(fresh.core.stores.has(`${fresh.userId}:${expected}`), true);
    await fresh.engine.purgeAccount(fresh.userId);
    assert.equal(fresh.core.stores.has(`${fresh.userId}:${expected}`), false);
  });

  // ---- bounded polling ----

  function fakeTimers() {
    const queue = [];
    return {
      queue,
      set(run, ms) { const handle = { run, ms, cancelled: false }; queue.push(handle); return handle; },
      clear(handle) { handle.cancelled = true; },
      async fire() { const next = queue.shift(); if (!next || next.cancelled) return false; next.run(); for (let i = 0; i < 20; i++) await Promise.resolve(); return true; },
      /** Fire until nothing is scheduled, but never more than `max` times: a poll with no bound must FAIL, not hang. */
      async drain(max = 200) { let n = 0; while (n < max && (await this.fire())) n++; return n; },
    };
  }

  test("the approval poll is bounded by its budget, never overlaps itself, and stops when done or stopped", async () => {
    const timers = fakeTimers();
    let ticks = 0;
    const poll = trust.startBoundedPoll({ intervalMs: 4000, budget: 5, timers, tick: async () => { ticks++; return false; } });
    const fired = await timers.drain();
    assert.equal(ticks, 5, "five ticks and then nothing more is scheduled");
    assert.equal(fired, 5);
    assert.equal(poll.ticks, 5);

    // A throwing tick counts against the budget and does not stop the poll early.
    const t2 = fakeTimers();
    let throws = 0;
    trust.startBoundedPoll({ intervalMs: 1, budget: 3, timers: t2, tick: async () => { throws++; throw new Error("x"); } });
    await t2.drain();
    assert.equal(throws, 3);

    // Done stops it; stop() stops it.
    const t3 = fakeTimers();
    let n = 0;
    trust.startBoundedPoll({ intervalMs: 1, budget: 50, timers: t3, tick: async () => ++n === 2 });
    await t3.drain();
    assert.equal(n, 2);
    const t4 = fakeTimers();
    let m = 0;
    const p4 = trust.startBoundedPoll({ intervalMs: 1, budget: 50, timers: t4, tick: async () => { m++; return false; } });
    await t4.fire();
    p4.stop();
    await t4.drain();
    assert.equal(m, 1);

    // A slow tick is not stacked: the next is scheduled only after it finishes.
    const t5 = fakeTimers();
    let release;
    let running = 0, overlap = 0;
    trust.startBoundedPoll({ intervalMs: 1, budget: 4, timers: t5, tick: () => new Promise((resolve) => { running++; overlap = Math.max(overlap, running); release = () => { running--; resolve(false); }; }) });
    await t5.fire();
    assert.equal(t5.queue.length, 0, "nothing scheduled while a tick is in flight");
    release();
    for (let i = 0; i < 20; i++) await Promise.resolve();
    assert.equal(t5.queue.length, 1);
    assert.equal(overlap, 1);
  });

  test("a pending phone becomes ready by being asked again, within the poll's budget", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    const timers = fakeTimers();
    const poll = trust.startBoundedPoll({ intervalMs: 4000, budget: 10, timers, tick: async () => (await alice2.engine.recheck("manual")).status !== "pending_approval" });
    await timers.fire();
    assert.equal(alice2.engine.getState().status, "pending_approval");
    await alice.engine.refreshPendingDevices();
    await alice.engine.approvePending(alice2.deviceId);
    await timers.fire();
    assert.equal(alice2.engine.getState().status, "ready");
    assert.equal(timers.queue.filter((t) => !t.cancelled).length, 0, "the poll stopped by itself");
    void poll;
  });

  // ---- privacy of what is logged ----

  test("diagnostics from the trust paths carry only place names and fixed codes", async () => {
    const world = new World();
    const { alice } = await world.pair();
    const alice2 = world.phone(alice.userId);
    await alice2.engine.start(alice.userId);
    world.server.down = true;
    await alice.engine.refreshPendingDevices();
    alice.engine.ports.api = { ...alice.engine.ports.api, listPendingDevicesV2: async () => { throw apiError(500, "Hello this is plaintext! canary"); } };
    world.server.down = false;
    await alice.engine.refreshPendingDevices();
    await alice2.engine.ownComparisonCode();
    assert.ok(alice.reports.length >= 2);
    assert.ok(alice.reports.every(([where, code]) => /^[a-z0-9_]+$/.test(where) && /^[a-z0-9_]+$/.test(code)), JSON.stringify(alice.reports));
    assert.ok(!JSON.stringify(alice.reports).includes("plaintext"));
  });

  return cases;
}

// ---------------------------------------------------------------------------
// Run, and the self-test
// ---------------------------------------------------------------------------

async function runAll(mutations = {}) {
  const loader = makeLoader(mutations);
  const cases = build(loader);
  const results = [];
  for (const { name, fn } of cases) {
    let deadline;
    try {
      await Promise.race([fn(), new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error(`case timed out: ${name}`)), 15_000); })]);
      results.push({ name, ok: true });
    } catch (error) {
      results.push({ name, ok: false, error });
    } finally {
      clearTimeout(deadline);
    }
  }
  return { results, applied: loader.applied };
}

const ENGINE = "e2eeEngine.ts";
const MUTATIONS = [
  ["a verified mark never goes stale", ENGINE, "return (await this.safetyNumberOf(session, peerUserId, mine, peer)) === marked;", "return true;"],
  ["safetyView ignores the stored number", ENGINE, 'verified: state === "trusted" && ownState === "trusted" && marked === number,', 'verified: marked !== null,'],
  ["setVerified ignores that the directories moved", ENGINE, 'if (view.fingerprint !== fingerprint) throw new E2eeTrustError("changed_again");', ""],
  ["setVerified marks during a hard stop", ENGINE, 'if (view.state !== "trusted" || view.ownState !== "trusted") throw new E2eeTrustError("blocked");', ""],
  ["accept ignores that the directory moved", ENGINE, 'if (expected !== null && directoryFingerprint(devices) !== expected) throw new E2eeTrustError("changed_again");', ""],
  ["accept never asks the core to accept", ENGINE, "await crypto.acceptIdentityChange(session.handle, userId);", "void 0;"],
  ["accept does not check again afterwards", ENGINE, "const after = await crypto.observeDirectory(session.handle, userId, devices);\n      this.emit", 'const after = "trusted";\n      this.emit'],
  ["accept is skipped when the core already shows trust (no re-pin of the observed set)", ENGINE, 'if (before !== "trusted") {', "if (false) {"],
  ["a hard stop no longer blocks sending", ENGINE, 'if (block !== null) throw new E2eeSendError("blocked", block);', ""],
  ["receiving starts checking trust", ENGINE, "const local = await crypto.listMessages(session.handle, conversationId, null, 200);", 'if ((await this.observeTrust(session, [{ userId: peerUserId, devices: peer }])) !== null) throw new E2eeSendError("blocked");\n    const local = await crypto.listMessages(session.handle, conversationId, null, 200);'],
  ["approve skips the core's signature", ENGINE, "approval_signature_b64: approval.signature,", 'approval_signature_b64: "forged",'],
  ["approve forgets to refresh the list", ENGINE, "this.emit({ type: \"trust\", userId: session.userId });\n      await this.refreshPendingDevices();", 'this.emit({ type: "trust", userId: session.userId });'],
  ["approve signs as another device", ENGINE, "approver_device_id: session.deviceId,", 'approver_device_id: target.deviceId,'],
  ["Not me does not revoke", ENGINE, "await this.ports.api.revokeDeviceV2(deviceId);\n      } catch (error) {\n        // Not found or not ours means it is not a pending phone", "void 0;\n      } catch (error) {\n        // Not found or not ours means it is not a pending phone"],
  ["the pending list includes this phone itself", ENGINE, "if (row.id === session.deviceId || row.suite !== E2EE_SUITE) continue;", "if (row.suite !== E2EE_SUITE) continue;"],
  ["Reset security revokes the current phone too", ENGINE, "ids.delete(session.deviceId);\n    return [...ids];", "return [...ids];"],
  ["Reset security registers before revoking the others", ENGINE, "for (let pass = 0; ; pass++) {\n      const others = await this.otherDeviceIds(session);", "for (let pass = 0; pass < 0; pass++) {\n      const others = await this.otherDeviceIds(session);"],
  ["Reset security loops without a bound", ENGINE, "if (pass >= MAX_RESET_REVOKE_PASSES) throw new E2eeTrustError(\"reset_incomplete\");", "if (pass >= 1000) throw new E2eeTrustError(\"reset_incomplete\");"],
  ["Reset security wipes before it has revoked anything", ENGINE, "    // 1. Revoke every other phone. Server side only, idempotent, nothing local changes yet.\n", "    await crypto.wipe(userId, session.generation);\n    // 1. Revoke every other phone. Server side only, idempotent, nothing local changes yet.\n"],
  ["Reset security forgets the abandoned pending phone", ENGINE, "if (session.deviceId !== registered.deviceId) await api.revokeDeviceV2(session.deviceId);", "void 0;"],
  ["Reset security ignores a registration that is not the root", ENGINE, "    if (!registered.approved) {\n      // The server did not make this the root", "    if (false) {\n      // The server did not make this the root"],
  ["a second Reset security tap starts another run", ENGINE, "if (this.resetting !== null) return this.resetting;\n    const session = this.session;\n    if (this.state.status !== \"pending_approval\"", "const session = this.session;\n    if (this.state.status !== \"pending_approval\""],
  ["the approval poll has no budget", "e2eeTrust.ts", "if (stopped || ticks >= options.budget) return;", "if (stopped) return;"],
  ["a failed registration leaves its store open for the retry to trip over", ENGINE, "unlocks the same namespace.\n      await this.lockQuietly(handle);\n      throw error;", "unlocks the same namespace.\n      throw error;"],
  ["a prompt outlives the phone's readiness", ENGINE, 'if (this.state.status !== "ready") this.setPending([]);', "void 0;"],
  ["a revoked device is restarted and claims to be ready", ENGINE, 'if (status === "error" && this.state.reason === DEVICE_REVOKED) return this.state;', "void 0;"],
  ["a stale directory is accepted for the safety number", ENGINE, "if (!strict && isRetryLater(error) && cached) return cached.devices;", "if (isRetryLater(error) && cached) return cached.devices;"],
];

const args = process.argv.slice(2);
if (args.includes("--self-test")) {
  // The plain suite must pass first: a self-test over a failing suite proves nothing.
  const baseline = await runAll();
  const baselineFailures = baseline.results.filter((r) => !r.ok);
  if (baselineFailures.length > 0) {
    console.log(`FAIL  the unmodified suite has ${baselineFailures.length} failure(s); fix those before trusting the self-test`);
    process.exit(1);
  }
  let missed = 0;
  for (const [name, file, from, to] of MUTATIONS) {
    let outcome;
    try {
      outcome = await runAll({ [file]: [from, to] });
    } catch (error) {
      console.log(`  BROKEN  ${name}\n          ${String(error.message ?? error)}`);
      missed++;
      continue;
    }
    const caught = outcome.results.filter((r) => !r.ok);
    if (outcome.applied.size === 0) {
      console.log(`  BROKEN  ${name}: the mutation was never applied`);
      missed++;
    } else if (caught.length === 0) {
      console.log(`  MISSED  ${name}`);
      missed++;
    } else {
      console.log(`  caught  ${name}  (${caught.length}: ${caught[0].name.slice(0, 70)})`);
    }
  }
  console.log(missed === 0 ? `\nOK  verify:e2ee-trust self-test - ${MUTATIONS.length} mutations, all caught` : `\n${missed} mutation(s) NOT caught`);
  process.exit(missed === 0 ? 0 : 1);
}

const { results } = await runAll();
let failures = 0;
for (const result of results) {
  if (result.ok) console.log(`  PASS  ${result.name}`);
  else {
    failures++;
    console.log(`  FAIL  ${result.name}\n        ${String(result.error?.stack ?? result.error).split("\n").slice(0, 6).join("\n        ")}`);
  }
}
console.log(failures === 0 ? `\nOK  verify:e2ee-trust - ${results.length} cases` : `\n${failures} FAILURE(S) of ${results.length}`);
process.exit(failures === 0 ? 0 : 1);
