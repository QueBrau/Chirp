/** Turn the encrypted local store plus the app-side extras into what a thread draws
 * (board c451). Pure: the engine supplies the pieces, a screen draws the result.
 */

import type { PlaintextMessage } from "./native";
import type { OutboxEntry, UndecryptableRow } from "./e2eeTypes";
import { comparePosition } from "./e2eeMapping";

/** `sending`: encrypted on this phone, not yet acknowledged. `failed`: will not go out unless the user retries. */
export type Delivery = "sent" | "sending" | "failed";

export interface ThreadMessage {
  kind: "message";
  key: string;
  mine: boolean;
  body: string;
  atMs: number;
  delivery: Delivery;
  clientMessageId: string;
}

/** Something arrived here that could not be decrypted. Says nothing about its content. */
export interface ThreadPlaceholder {
  kind: "undecryptable";
  key: string;
}

export type ThreadItem = ThreadMessage | ThreadPlaceholder;

export interface ThreadInput {
  /** The core's page, newest first. */
  messages: readonly PlaintextMessage[];
  /** More local messages exist before this page. */
  hasOlder: boolean;
  outbox: readonly OutboxEntry[];
  undecryptable: readonly UndecryptableRow[];
  myUserId: string;
}

/**
 * Oldest first, so the newest message is last. Order is local commit order (`seq`): a
 * received message sits where this phone decrypted it, which for a live conversation is
 * where it arrived. A placeholder is drawn right after the message it was anchored to.
 */
export function buildThread(input: ThreadInput): ThreadItem[] {
  const ascending = [...input.messages].sort((a, b) => a.seq - b.seq);
  const firstSeq = ascending.length > 0 ? ascending[0].seq : 0;
  const outbox = new Map(input.outbox.map((entry) => [entry.clientMessageId, entry]));

  // When older history is not loaded, a placeholder anchored before this page would be
  // drawn above its neighbours; leave it for the page that contains its anchor.
  const placeholders = input.undecryptable
    .filter((row) => !input.hasOlder || row.anchorSeq >= firstSeq)
    .sort((a, b) => a.anchorSeq - b.anchorSeq || comparePosition({ created_at: a.createdAt, id: a.messageId }, { created_at: b.createdAt, id: b.messageId }));

  const items: ThreadItem[] = [];
  let next = 0;
  const flushThrough = (seq: number) => {
    while (next < placeholders.length && placeholders[next].anchorSeq <= seq) {
      items.push({ kind: "undecryptable", key: `x-${placeholders[next].messageId}` });
      next += 1;
    }
  };
  // Anchors before the first message of this page come first (nothing to attach to).
  flushThrough(firstSeq - 1);
  for (const message of ascending) {
    const entry = outbox.get(message.clientMessageId);
    const delivery: Delivery =
      message.direction === "outgoing" && entry !== undefined ? (entry.status === "failed" ? "failed" : "sending") : "sent";
    items.push({
      kind: "message",
      key: `m-${message.seq}`,
      mine: message.senderUserId === input.myUserId,
      body: message.body,
      atMs: message.storedAtMs,
      delivery,
      clientMessageId: message.clientMessageId,
    });
    flushThrough(message.seq);
  }
  flushThrough(Number.MAX_SAFE_INTEGER);
  return items;
}
