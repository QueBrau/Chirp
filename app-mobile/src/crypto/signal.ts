/** libsignal wrapper: session management + encrypt/decrypt (SPEC §6.2, §6.5).
 *
 * TYPED STUB — every function throws until the milestone-3 libsignal spike lands
 * (`@signalapp/libsignal-client`, dev build required). NO fake crypto here, ever:
 * a stub that "encrypts" would be worse than one that throws (CONVENTIONS).
 */

import type { DevicePrekeyBundleOut } from "../api/keys";
import type { MessageType } from "../api/messages";

/** A remote Signal endpoint: one (user, device) pair. */
export interface SignalAddress {
  userId: string;
  deviceId: string;
}

/** Output of encryption: opaque base64 ciphertext + protocol message type. */
export interface EncryptedEnvelope {
  ciphertextB64: string;
  messageType: MessageType;
}

/** Whether a Double Ratchet session already exists with the given device. */
export async function hasSession(address: SignalAddress): Promise<boolean> {
  throw new Error("TODO(milestone-3): libsignal");
}

/**
 * X3DH: establish a session from a fetched prekey bundle (GET /users/{id}/prekey-bundle),
 * consuming the bundle's one-time prekey if present.
 */
export async function establishSession(
  address: SignalAddress,
  bundle: DevicePrekeyBundleOut,
): Promise<void> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Encrypt plaintext for one device over the Double Ratchet session. */
export async function encryptMessage(
  address: SignalAddress,
  plaintext: string,
): Promise<EncryptedEnvelope> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Decrypt an incoming envelope from one device; plaintext goes ONLY to local SQLite. */
export async function decryptMessage(
  address: SignalAddress,
  envelope: EncryptedEnvelope,
): Promise<string> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Tear down the session with a device (e.g. after device revocation). */
export async function endSession(address: SignalAddress): Promise<void> {
  throw new Error("TODO(milestone-3): libsignal");
}
