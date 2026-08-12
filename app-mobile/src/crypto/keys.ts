/** Key management: identity keygen, Keychain/Keystore storage, prekey replenishment (SPEC §6.1).
 *
 * TYPED STUB — throws until milestone 3. Private keys NEVER leave the device
 * (SPEC §8.5): everything returned here is public material shaped for the
 * key-directory API (src/api/keys.ts).
 */

import type { DeviceCreate, OneTimePrekeyCreate, SignedPrekeyCreate } from "../api/keys";

/** Public half of the locally-stored identity; private keys stay in Keychain/Keystore. */
export interface StoredIdentity {
  registrationId: number;
  identityKeyB64: string;
}

/** Default one-time prekey batch size at registration (SPEC §6.1: ~100). */
export const INITIAL_ONE_TIME_PREKEY_COUNT = 100;

/** Replenish when the server reports fewer than this many unconsumed OTKs. */
export const PREKEY_REPLENISH_THRESHOLD = 20;

/** Generate identity keypair + registration id; store private half in Keychain/Keystore. */
export async function generateIdentity(): Promise<StoredIdentity> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Load the stored identity's public half, or null if this device has not registered. */
export async function loadIdentity(): Promise<StoredIdentity | null> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Generate a fresh signed prekey (public + signature) for upload. */
export async function generateSignedPrekey(keyId: number): Promise<SignedPrekeyCreate> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Generate a batch of one-time prekeys starting at `startKeyId`. */
export async function generateOneTimePrekeys(
  startKeyId: number,
  count: number,
): Promise<OneTimePrekeyCreate[]> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Build the full POST /devices registration payload for a brand-new device. */
export async function buildDeviceRegistration(
  deviceLabel: string | null,
): Promise<DeviceCreate> {
  throw new Error("TODO(milestone-3): libsignal");
}

/**
 * Check the server's unconsumed-OTK count and upload a fresh batch when it drops
 * below PREKEY_REPLENISH_THRESHOLD (SPEC §6.1).
 */
export async function replenishOneTimePrekeysIfLow(deviceId: string): Promise<void> {
  throw new Error("TODO(milestone-3): libsignal");
}
