/** Typed bridge to the native E2EE core (board c448, milestone M3; design sections 2 and 3).
 *
 * `modules/chirp-crypto` is an Expo local module, iOS only for now, that wraps the Rust
 * core (vodozemac Olm + an encrypted store). This file is the ONLY place JavaScript
 * touches it. It adds three things on top of the raw module and nothing else:
 *
 *  - exact TypeScript types for every record the module takes and returns;
 *  - `E2EEUnavailableError` when the module is absent (web, Android, Expo Go, a dev
 *    build cut before the module existed), instead of a crash at import time;
 *  - `E2EEError`, whose `code` is one of the core's fixed error codes.
 *
 * It does NOT make anything encrypt on its own: nothing in the app calls it yet, and
 * `signal.ts`, `keys.ts` and `groups.ts` are untouched, so `verify:e2ee-claims` keeps
 * guarding the copy. Private keys and the store key never cross this boundary; JS sees
 * public keys, ciphertext, and plaintext that is already durably committed.
 *
 * Conventions: ids are lowercase hyphenated UUID text, binary values are base64 text,
 * enums are snake_case strings, counters (`keyId`, `seq`, timestamps) are JS numbers
 * (they stay far below 2^53). `before`/`limit` follow `listMessages` below.
 */

import { requireOptionalNativeModule } from "expo";

// ---------------------------------------------------------------------------
// Scalar aliases and enums
// ---------------------------------------------------------------------------

/** Lowercase hyphenated UUID, `8-4-4-4-12`. Inputs are lowercased for you. */
export type Uuid = string;
/** Standard-alphabet base64. Output is padded; input may be padded or not. */
export type Base64 = string;

export type KeyKind = "one_time" | "fallback";
export type TrustState = "trusted" | "identity_changed" | "unapproved_device";
export type Direction = "incoming" | "outgoing";

/**
 * Opaque id of one unlocked account store. Returned by `unlock`, dead after `lock` or
 * `wipe`. Several can be open at once (different accounts); the same account twice is
 * `store_in_use`.
 */
export type CryptoHandle = string & { readonly __brand: "ChirpCryptoHandle" };

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

export interface PublicIdentity {
  curve25519: Base64;
  ed25519: Base64;
}

/** A public one-time or fallback key with its owning device's signature over it. */
export interface SignedKey {
  keyId: number;
  kind: KeyKind;
  publicKey: Base64;
  signature: Base64;
}

export interface CreatedDevice {
  identity: PublicIdentity;
  bindingSignature: Base64;
  oneTimeKeys: SignedKey[];
  fallbackKey: SignedKey;
}

/** An already-approved device of the same account vouching for a new one. */
export interface DeviceApproval {
  approverEd25519: Base64;
  signature: Base64;
}

/** One device from a user's directory, as read from the server. `approval` null = root. */
export interface DirectoryDevice {
  userId: Uuid;
  deviceId: Uuid;
  generation: number;
  identityCurve25519: Base64;
  identityEd25519: Base64;
  bindingSignature: Base64;
  approval: DeviceApproval | null;
}

/** What `approveDevice` needs: the new device's public binding. */
export interface NewDeviceBinding {
  userId: Uuid;
  generation: number;
  identityCurve25519: Base64;
  identityEd25519: Base64;
  bindingSignature: Base64;
}

/** A recipient device. `claimedKey` is only needed when there is no session with it yet. */
export interface Recipient {
  device: DirectoryDevice;
  claimedKey: SignedKey | null;
}

/** One ciphertext leg. `olmType` is 0 (prekey message) or 1 (normal). */
export interface Leg {
  recipientDeviceId: Uuid;
  olmType: 0 | 1;
  ciphertext: Base64;
}

/** The routing a received leg is expected to carry; all six fields are checked. */
export interface ExpectedEnvelope {
  conversationId: Uuid;
  clientMessageId: Uuid;
  senderUserId: Uuid;
  senderDeviceId: Uuid;
  recipientUserId: Uuid;
  recipientDeviceId: Uuid;
}

/** A committed plaintext message from the local store. */
export interface PlaintextMessage {
  /** Local, store-assigned, strictly increasing. Cursor for `listMessages`. */
  seq: number;
  direction: Direction;
  /** Server message id; null for messages this device sent. */
  messageId: Uuid | null;
  conversationId: Uuid;
  clientMessageId: Uuid;
  senderUserId: Uuid;
  senderDeviceId: Uuid;
  /** Always "text" in v1. */
  kind: string;
  body: string;
  /** The sender device's standing against our pins when this was received. */
  senderTrust: TrustState;
  /** Local wall clock at commit, ms since the epoch. Display only. */
  storedAtMs: number;
}

export interface KeyStatus {
  /** Generated but not yet confirmed published via `markPublished`. */
  pending: number;
  /** Published and not yet consumed by a peer's prekey message. */
  publishedOneTime: number;
  hasFallback: boolean;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** The core's fixed error codes (Rust `Error::code`), plus the two raised natively. */
export const E2EE_ERROR_CODES = [
  "locked",
  "unlock_failed",
  "store_in_use",
  "storage_failed",
  "store_corrupt",
  "namespace_mismatch",
  "device_exists",
  "no_device",
  "not_published",
  "invalid_input",
  "body_invalid",
  "body_too_large",
  "invalid_signature",
  "invalid_key",
  "unknown_key",
  "too_many_keys",
  "sender_mismatch",
  "missing_claimed_key",
  "trust_hard_stop",
  "unknown_peer",
  "nothing_to_accept",
  "client_message_id_reused",
  "ciphertext_too_large",
  "encrypt_failed",
  "decrypt_failed",
  "invalid_envelope",
  "envelope_mismatch",
  "replay",
  "internal",
  /** The store is older than the generation pinned in the Keychain (a restored copy). */
  "store_rolled_back",
  /** The Keychain failed. */
  "keychain_failed",
] as const;

export type E2EENativeErrorCode = (typeof E2EE_ERROR_CODES)[number];
/** `e2ee_unavailable` is raised here, never by the native module. */
export type E2EEErrorCode = E2EENativeErrorCode | "e2ee_unavailable";

const KNOWN_CODES: ReadonlySet<string> = new Set(E2EE_ERROR_CODES);

/** A failure of an E2EE call. The code is the whole story: there is no native message. */
export class E2EEError extends Error {
  readonly code: E2EEErrorCode;

  constructor(code: E2EEErrorCode) {
    super(code);
    this.name = "E2EEError";
    this.code = code;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** The native module is not in this build (web, Android, Expo Go, or an older dev build). */
export class E2EEUnavailableError extends E2EEError {
  constructor() {
    super("e2ee_unavailable");
    this.name = "E2EEUnavailableError";
  }
}

/**
 * Native rejections carry the fixed code on `.code`. Anything else (an argument the
 * Expo layer could not convert, a runtime fault) becomes `internal`: the original
 * message may quote caller data, so it is dropped.
 */
function toE2EEError(error: unknown): E2EEError {
  if (error instanceof E2EEError) return error;
  const code = (error as { code?: unknown } | null)?.code;
  if (typeof code === "string" && KNOWN_CODES.has(code)) {
    return new E2EEError(code as E2EENativeErrorCode);
  }
  return new E2EEError("internal");
}

// ---------------------------------------------------------------------------
// The raw native module (positional arguments, handles as plain strings)
// ---------------------------------------------------------------------------

interface ChirpCryptoNative {
  unlock(userId: string, generation: number): Promise<string>;
  lock(handle: string): Promise<void>;
  wipe(userId: string, generation: number): Promise<void>;
  createDevice(handle: string, otkCount: number): Promise<CreatedDevice>;
  markPublished(handle: string, serverDeviceId: string, keyIds: number[]): Promise<void>;
  topUpKeys(handle: string, count: number): Promise<SignedKey[]>;
  pendingKeys(handle: string): Promise<SignedKey[]>;
  rotateFallbackKey(handle: string): Promise<SignedKey>;
  keyStatus(handle: string): Promise<KeyStatus>;
  publicIdentity(handle: string): Promise<PublicIdentity | null>;
  observeDirectory(handle: string, userId: string, devices: NativeDirectoryDevice[]): Promise<TrustState>;
  trustState(handle: string, peerUserId: string): Promise<TrustState>;
  acceptIdentityChange(handle: string, peerUserId: string): Promise<void>;
  approveDevice(handle: string, newDevice: NewDeviceBinding): Promise<DeviceApproval>;
  safetyNumber(
    myUserId: string,
    myIdentities: string[],
    peerUserId: string,
    peerIdentities: string[],
  ): Promise<string>;
  encrypt(
    handle: string,
    conversationId: string,
    clientMessageId: string,
    body: string,
    sender: NativeDirectoryDevice,
    recipients: NativeRecipient[],
  ): Promise<Leg[]>;
  decrypt(
    handle: string,
    messageId: string,
    leg: Leg,
    sender: NativeDirectoryDevice,
    expected: ExpectedEnvelope,
  ): Promise<PlaintextMessage>;
  listMessages(
    handle: string,
    conversationId: string,
    before: number | null,
    limit: number,
  ): Promise<PlaintextMessage[]>;
}

/** Optional fields are omitted rather than sent as null (the Expo record treats absent as nil). */
type NativeDirectoryDevice = Omit<DirectoryDevice, "approval"> & { approval?: DeviceApproval };
type NativeRecipient = { device: NativeDirectoryDevice; claimedKey?: SignedKey };

let cached: ChirpCryptoNative | null | undefined;

function nativeModule(): ChirpCryptoNative {
  if (cached === undefined) {
    cached = requireOptionalNativeModule<ChirpCryptoNative>("ChirpCrypto");
  }
  if (cached === null) throw new E2EEUnavailableError();
  return cached;
}

/** True when this build includes the native module. Safe to call anywhere, any time. */
export function isE2EEAvailable(): boolean {
  try {
    nativeModule();
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Input normalisation: ids are lowercased and checked before they cross the boundary
// ---------------------------------------------------------------------------

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function uuid(value: string): Uuid {
  const lower = typeof value === "string" ? value.toLowerCase() : "";
  if (!UUID_RE.test(lower)) throw new E2EEError("invalid_input");
  return lower;
}

function wholeNumber(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) throw new E2EEError("invalid_input");
  return value;
}

function nativeDevice(device: DirectoryDevice): NativeDirectoryDevice {
  const { approval, ...rest } = device;
  return {
    ...rest,
    userId: uuid(device.userId),
    deviceId: uuid(device.deviceId),
    generation: wholeNumber(device.generation),
    ...(approval ? { approval } : {}),
  };
}

function nativeRecipient(recipient: Recipient): NativeRecipient {
  return {
    device: nativeDevice(recipient.device),
    ...(recipient.claimedKey ? { claimedKey: recipient.claimedKey } : {}),
  };
}

function nativeLeg(leg: Leg): Leg {
  return { ...leg, recipientDeviceId: uuid(leg.recipientDeviceId) };
}

function nativeExpected(e: ExpectedEnvelope): ExpectedEnvelope {
  return {
    conversationId: uuid(e.conversationId),
    clientMessageId: uuid(e.clientMessageId),
    senderUserId: uuid(e.senderUserId),
    senderDeviceId: uuid(e.senderDeviceId),
    recipientUserId: uuid(e.recipientUserId),
    recipientDeviceId: uuid(e.recipientDeviceId),
  };
}

/** Run one native call; every failure leaves as an `E2EEError`. */
async function call<T>(run: (native: ChirpCryptoNative) => Promise<T>): Promise<T> {
  try {
    return await run(nativeModule());
  } catch (error) {
    throw toE2EEError(error);
  }
}

// ---------------------------------------------------------------------------
// The API
// ---------------------------------------------------------------------------

/**
 * Every call runs on ONE serial native queue, so calls are processed in the order they
 * are made. Do not rely on two un-awaited calls overlapping.
 */
export const chirpCrypto = {
  /**
   * Open (creating if new) the store for one account namespace. The store key is made
   * and kept natively in the Keychain and is never returned. Fails with
   * `store_rolled_back` if an older copy of the store was restored, `unlock_failed` if the
   * key is gone or wrong, `store_in_use` if this namespace is already open.
   */
  unlock(userId: Uuid, generation: number): Promise<CryptoHandle> {
    return call(async (n) => (await n.unlock(uuid(userId), wholeNumber(generation))) as CryptoHandle);
  },

  /** Close the store. Idempotent. The handle is dead afterwards; `unlock` again for a new one. */
  lock(handle: CryptoHandle): Promise<void> {
    return call((n) => n.lock(handle));
  },

  /**
   * Delete a namespace's store files AND both Keychain items (store key and generation
   * pin), closing any open handle for it first. Idempotent; works without unlocking.
   */
  wipe(userId: Uuid, generation: number): Promise<void> {
    return call((n) => n.wipe(uuid(userId), wholeNumber(generation)));
  },

  /** Create this namespace's device: identity, binding signature, signed keys. */
  createDevice(handle: CryptoHandle, otkCount: number): Promise<CreatedDevice> {
    return call((n) => n.createDevice(handle, wholeNumber(otkCount)));
  },

  /** Record the server's id for this device and which keys the server stored. */
  markPublished(handle: CryptoHandle, serverDeviceId: Uuid, keyIds: number[]): Promise<void> {
    return call((n) => n.markPublished(handle, uuid(serverDeviceId), keyIds.map(wholeNumber)));
  },

  /** Generate and sign `count` more one-time keys. */
  topUpKeys(handle: CryptoHandle, count: number): Promise<SignedKey[]> {
    return call((n) => n.topUpKeys(handle, wholeNumber(count)));
  },

  /** Signed keys generated but not yet confirmed via `markPublished`. */
  pendingKeys(handle: CryptoHandle): Promise<SignedKey[]> {
    return call((n) => n.pendingKeys(handle));
  },

  /** Generate and sign a new fallback key. */
  rotateFallbackKey(handle: CryptoHandle): Promise<SignedKey> {
    return call((n) => n.rotateFallbackKey(handle));
  },

  keyStatus(handle: CryptoHandle): Promise<KeyStatus> {
    return call((n) => n.keyStatus(handle));
  },

  /** This device's public identity, or null if no device has been created. */
  publicIdentity(handle: CryptoHandle): Promise<PublicIdentity | null> {
    return call((n) => n.publicIdentity(handle));
  },

  /** Record a peer user's current device directory; returns the resulting trust state. */
  observeDirectory(
    handle: CryptoHandle,
    userId: Uuid,
    devices: DirectoryDevice[],
  ): Promise<TrustState> {
    return call((n) => n.observeDirectory(handle, uuid(userId), devices.map(nativeDevice)));
  },

  /** The state from the last observation of this user. `unknown_peer` if never seen. */
  trustState(handle: CryptoHandle, peerUserId: Uuid): Promise<TrustState> {
    return call((n) => n.trustState(handle, uuid(peerUserId)));
  },

  /** Re-pin exactly the directory last observed for this peer. */
  acceptIdentityChange(handle: CryptoHandle, peerUserId: Uuid): Promise<void> {
    return call((n) => n.acceptIdentityChange(handle, uuid(peerUserId)));
  },

  /** Sign the approval of a new device of THIS account. */
  approveDevice(handle: CryptoHandle, newDevice: NewDeviceBinding): Promise<DeviceApproval> {
    return call((n) =>
      n.approveDevice(handle, {
        ...newDevice,
        userId: uuid(newDevice.userId),
        generation: wholeNumber(newDevice.generation),
      }),
    );
  },

  /**
   * The 60-digit safety number from each side's approved device Ed25519 identities.
   * Identical on both sides. Needs no handle.
   */
  safetyNumber(
    myUserId: Uuid,
    myIdentities: Base64[],
    peerUserId: Uuid,
    peerIdentities: Base64[],
  ): Promise<string> {
    return call((n) => n.safetyNumber(uuid(myUserId), myIdentities, uuid(peerUserId), peerIdentities));
  },

  /** One ciphertext leg per recipient device. Committed before it returns. */
  encrypt(
    handle: CryptoHandle,
    conversationId: Uuid,
    clientMessageId: Uuid,
    body: string,
    sender: DirectoryDevice,
    recipients: Recipient[],
  ): Promise<Leg[]> {
    return call((n) =>
      n.encrypt(
        handle,
        uuid(conversationId),
        uuid(clientMessageId),
        body,
        nativeDevice(sender),
        recipients.map(nativeRecipient),
      ),
    );
  },

  /** Decrypt one leg addressed to this device. Committed before the plaintext returns. */
  decrypt(
    handle: CryptoHandle,
    messageId: Uuid,
    leg: Leg,
    sender: DirectoryDevice,
    expected: ExpectedEnvelope,
  ): Promise<PlaintextMessage> {
    return call((n) =>
      n.decrypt(handle, uuid(messageId), nativeLeg(leg), nativeDevice(sender), nativeExpected(expected)),
    );
  },

  /**
   * Newest-first page of a conversation from the local store. `before` is exclusive
   * (pass the smallest `seq` of the previous page); null starts at the newest.
   */
  listMessages(
    handle: CryptoHandle,
    conversationId: Uuid,
    before: number | null,
    limit: number,
  ): Promise<PlaintextMessage[]> {
    return call((n) =>
      n.listMessages(
        handle,
        uuid(conversationId),
        before === null ? null : wholeNumber(before),
        wholeNumber(limit),
      ),
    );
  },
} as const;
