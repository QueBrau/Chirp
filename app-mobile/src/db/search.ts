/** Local message search over the on-device store — the server can't search ciphertext (SPEC §2.1). */

import type { SQLiteDatabase } from "expo-sqlite";

import type { LocalMessageRow } from "./schema";

/**
 * Search decrypted local history (optionally scoped to one conversation).
 * TODO(milestone-4): implement over the messages table (LIKE now, FTS5 later).
 */
export async function searchLocalMessages(
  db: SQLiteDatabase,
  query: string,
  options: { conversationId?: string; limit?: number } = {},
): Promise<LocalMessageRow[]> {
  throw new Error("TODO(milestone-4): local message search");
}
