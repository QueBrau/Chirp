/** expo-sqlite local store: decrypted messages + offline outbox (SPEC §5, §2.1).
 *
 * Decrypted message history lives ONLY here, on-device — never on the server.
 * The outbox holds unsent plaintext until send time, when it is encrypted and
 * POSTed as ciphertext (flushed by src/realtime/queue.ts in milestone 4).
 */

import * as SQLite from "expo-sqlite";

export const DB_NAME = "chirp.db";

/** Decrypted message history — device-only (SPEC §2.1). */
export const CREATE_MESSAGES_TABLE = `
CREATE TABLE IF NOT EXISTS messages (
  id               TEXT PRIMARY KEY NOT NULL, -- server message id
  conversation_id  TEXT NOT NULL,
  sender_user_id   TEXT NOT NULL,
  sender_device_id TEXT NOT NULL,
  body             TEXT NOT NULL,             -- decrypted plaintext (device-only)
  message_type     TEXT NOT NULL DEFAULT 'signal',
  created_at       TEXT NOT NULL,             -- ISO 8601
  read_at          TEXT
);`;

export const CREATE_MESSAGES_INDEX = `
CREATE INDEX IF NOT EXISTS idx_local_messages_convo_time
  ON messages (conversation_id, created_at DESC);`;

/** Offline outbox: plaintext queued for encryption + send on reconnect. */
export const CREATE_OUTBOX_TABLE = `
CREATE TABLE IF NOT EXISTS outbox (
  id               TEXT PRIMARY KEY NOT NULL, -- client-generated id
  conversation_id  TEXT NOT NULL,
  plaintext        TEXT NOT NULL,             -- encrypted at send time, never uploaded as-is
  message_type     TEXT NOT NULL DEFAULT 'signal',
  created_at       TEXT NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT
);`;

export interface LocalMessageRow {
  id: string;
  conversation_id: string;
  sender_user_id: string;
  sender_device_id: string;
  body: string;
  message_type: "signal" | "sender_key_distribution";
  created_at: string;
  read_at: string | null;
}

export interface OutboxRow {
  id: string;
  conversation_id: string;
  plaintext: string;
  message_type: "signal" | "sender_key_distribution";
  created_at: string;
  attempts: number;
  last_error: string | null;
}

/** Create all tables + indexes; idempotent (IF NOT EXISTS throughout). */
export async function initSchema(db: SQLite.SQLiteDatabase): Promise<void> {
  await db.execAsync(
    ["PRAGMA journal_mode = WAL;", CREATE_MESSAGES_TABLE, CREATE_MESSAGES_INDEX, CREATE_OUTBOX_TABLE].join(
      "\n",
    ),
  );
}

let dbPromise: Promise<SQLite.SQLiteDatabase> | null = null;

/** Open (once) and initialize the local database; safe to call from anywhere. */
export function openDb(): Promise<SQLite.SQLiteDatabase> {
  if (!dbPromise) {
    dbPromise = SQLite.openDatabaseAsync(DB_NAME).then(async (db) => {
      await initSchema(db);
      return db;
    });
  }
  return dbPromise;
}
