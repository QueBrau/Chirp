/** Offline outbox: queue plaintext locally, encrypt + send on reconnect (SPEC §5).
 *
 * TYPED STUB — implementation lands in milestone 4 alongside E2EE messaging.
 * Backing store is the `outbox` table in src/db/schema.ts; flush() encrypts via
 * src/crypto and POSTs ciphertext through src/api/messages.
 */

import type { MessageType } from "../api/messages";
import type { OutboxRow } from "../db/schema";

export interface EnqueueInput {
  conversation_id: string;
  plaintext: string;
  message_type?: MessageType;
}

/** The offline outbox contract consumed by the messages UI and the socket layer. */
export interface OfflineOutbox {
  /** Persist an unsent message locally; returns the queued row. */
  enqueue(input: EnqueueInput): Promise<OutboxRow>;
  /** All rows still awaiting send, oldest first. */
  pending(): Promise<OutboxRow[]>;
  /** Encrypt + send every pending row; resolves to the number successfully sent. */
  flush(): Promise<number>;
  /** Record a failed attempt (increments attempts, stores last_error). */
  markFailed(id: string, error: string): Promise<void>;
  /** Drop a row (sent successfully, or abandoned by the user). */
  remove(id: string): Promise<void>;
}

/** Stub factory — every method throws until milestone 4. */
export function createOfflineOutbox(): OfflineOutbox {
  const todo = (): never => {
    throw new Error("TODO(milestone-4): offline outbox");
  };
  return {
    enqueue: async () => todo(),
    pending: async () => todo(),
    flush: async () => todo(),
    markFailed: async () => todo(),
    remove: async () => todo(),
  };
}
