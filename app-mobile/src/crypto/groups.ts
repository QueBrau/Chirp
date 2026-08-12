/** Group E2EE: sender-key create/distribute/rotate-on-leave (SPEC §6.3–§6.4).
 *
 * TYPED STUB — throws until milestone 3. Rotation on member leave is the most
 * security-critical client behavior in the app: when the server sets left_at on
 * a conversation member, remaining clients MUST rotate and redistribute.
 */

import type { SignalAddress } from "./signal";

/** A pairwise-encrypted sender-key distribution envelope destined for one device. */
export interface SenderKeyDistribution {
  recipient: SignalAddress;
  /** Ciphertext for a message with message_type="sender_key_distribution". */
  ciphertextB64: string;
}

/** Create this device's sender key for a group conversation. */
export async function createSenderKey(conversationId: string): Promise<void> {
  throw new Error("TODO(milestone-3): libsignal");
}

/**
 * Encrypt the sender key pairwise (Double Ratchet) for every member device, for
 * delivery as `sender_key_distribution` messages (SPEC §6.3).
 */
export async function createDistributionMessages(
  conversationId: string,
  memberDevices: SignalAddress[],
): Promise<SenderKeyDistribution[]> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Process a received sender_key_distribution message from another member device. */
export async function processSenderKeyDistribution(
  conversationId: string,
  sender: SignalAddress,
  ciphertextB64: string,
): Promise<void> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Encrypt a group message once with this device's sender key. */
export async function encryptGroupMessage(
  conversationId: string,
  plaintext: string,
): Promise<string> {
  throw new Error("TODO(milestone-3): libsignal");
}

/** Decrypt a group message using the sender's distributed key. */
export async function decryptGroupMessage(
  conversationId: string,
  sender: SignalAddress,
  ciphertextB64: string,
): Promise<string> {
  throw new Error("TODO(milestone-3): libsignal");
}

/**
 * SECURITY-CRITICAL (SPEC §6.4): after a member leaves or is kicked (left_at set),
 * generate a fresh sender key and redistribute to the remaining devices so the
 * departed member cannot read new messages. Test this first in milestone 3.
 */
export async function rotateSenderKeyOnLeave(
  conversationId: string,
  remainingDevices: SignalAddress[],
): Promise<SenderKeyDistribution[]> {
  throw new Error("TODO(milestone-3): libsignal");
}
