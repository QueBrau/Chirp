// Small shared helpers: base64 <-> bytes, pretty logging, and the "which parser"
// decrypt-with-fallback helper (see FINDINGS.md #2 — message_type doesn't
// distinguish Signal's PreKey vs Whisper ciphertext, so the receiver has to work
// it out itself).

import * as Signal from '@signalapp/libsignal-client';

export const toB64 = (bytes) => Buffer.from(bytes).toString('base64');
export const fromB64 = (b64) => new Uint8Array(Buffer.from(b64, 'base64'));

export function log(...args) {
  // eslint-disable-next-line no-console
  console.log(...args);
}

export function section(title) {
  log(`\n=== ${title} ===`);
}

/**
 * Decrypt an inbound 1:1 ciphertext without an out-of-band type hint.
 *
 * Real Signal envelopes carry an explicit message type (Whisper vs PreKey) from the
 * server; Chirp's `messages.message_type` column only distinguishes "signal" from
 * "sender_key_distribution" (see FINDINGS.md #2), so nothing tells the receiver which
 * of the two wire formats it got. We recover by trying the PreKeySignalMessage parse
 * first (only valid for the very first message on a session) and falling back to a
 * plain SignalMessage parse. This is empirically safe: the two are different
 * protobufs and cross-parsing throws rather than silently returning garbage
 * (verified in this spike — see FINDINGS.md).
 */
export async function decryptInbound(ciphertextBytes, senderAddress, localAddress, stores) {
  try {
    const preKeyMessage = Signal.PreKeySignalMessage.deserialize(ciphertextBytes);
    const plaintext = await Signal.signalDecryptPreKey(
      preKeyMessage,
      senderAddress,
      localAddress,
      stores.session,
      stores.identity,
      stores.preKey,
      stores.signedPreKey,
      stores.kyberPreKey,
    );
    return { plaintext, wireType: 'PreKeySignalMessage' };
  } catch (preKeyError) {
    const whisperMessage = Signal.SignalMessage.deserialize(ciphertextBytes);
    const plaintext = await Signal.signalDecrypt(
      whisperMessage,
      senderAddress,
      localAddress,
      stores.session,
      stores.identity,
    );
    return { plaintext, wireType: 'SignalMessage', preKeyParseError: String(preKeyError.message ?? preKeyError) };
  }
}
