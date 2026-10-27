/** Durable local erasure journal (c452). It contains identifiers, never message text.
 *
 * Reads and writes deliberately fail closed. A missing journal is new; an unreadable
 * or malformed journal is NOT evidence that there is no deletion to finish.
 */
import type { KvPort } from "./e2eeStore";

export type ConversationCleanupPhase = "requested" | "confirmed" | "complete";
export interface CleanupState {
  account: { generations: number[]; complete: boolean } | null;
  conversations: Record<string, ConversationCleanupPhase>;
}
const fresh = (): CleanupState => ({ account: null, conversations: {} });

function parse(raw: string | null): CleanupState {
  if (raw === null) return fresh();
  const value = JSON.parse(raw) as CleanupState;
  if (value === null || typeof value !== "object" || !Object.hasOwn(value, "account") ||
      value.conversations === null || typeof value.conversations !== "object" || Array.isArray(value.conversations)) {
    throw new Error("cleanup_journal_invalid");
  }
  if (value.account !== null && (typeof value.account !== "object" ||
      !Array.isArray(value.account.generations) ||
      !value.account.generations.every(g => Number.isSafeInteger(g) && g > 0) ||
      typeof value.account.complete !== "boolean")) throw new Error("cleanup_journal_invalid");
  for (const [id, phase] of Object.entries(value.conversations)) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ||
        !["requested", "confirmed", "complete"].includes(phase)) throw new Error("cleanup_journal_invalid");
  }
  return value;
}

export class CleanupJournal {
  private tail: Promise<unknown> = Promise.resolve();
  private state: CleanupState | null = null;
  private readonly key: string;
  constructor(private readonly kv: KvPort, userId: string) {
    this.key = `chirp.e2ee.cleanup.${userId}`;
  }
  load(): Promise<CleanupState> {
    // Reads share the writer queue too: a slow first read must not overwrite a
    // newer confirmed intent in the cache after its durable write has finished.
    const read = this.tail.then(async () => {
      if (this.state === null) this.state = parse(await this.kv.get(this.key));
      return this.state;
    });
    this.tail = read.then(() => undefined, () => undefined);
    return read;
  }
  update(change: (state: CleanupState) => void): Promise<void> {
    const write = this.tail.then(async () => {
      const current = this.state ?? parse(await this.kv.get(this.key));
      const next = JSON.parse(JSON.stringify(current)) as CleanupState;
      change(next);
      await this.kv.set(this.key, JSON.stringify(next));
      this.state = next;
    });
    this.tail = write.catch(() => undefined);
    return write;
  }
}
