/** Messaging API (ciphertext only): conversations, messages, receipts — routers/messages.py.
 *
 * The server never sees plaintext: ciphertext_b64 is an opaque base64 blob (SPEC §2.1).
 * Decrypted history lives ONLY in the on-device SQLite store (src/db/schema.ts).
 */

import { request, type RequestOptions } from "./client";
export type MessageRequestOptions = Pick<RequestOptions, "operation" | "signal" | "timeoutMs">;

export type ConversationKind = "dm" | "group";
export type MessageType = "signal" | "sender_key_distribution";

/** Mirrors backend ConversationCreate — member_user_ids are the other participants. */
export interface ConversationCreate {
  chapter_id?: string | null;
  kind: ConversationKind;
  title?: string | null;
  member_user_ids: string[];
}

export interface ConversationMemberOut {
  conversation_id: string;
  user_id: string;
  joined_at: string;
  left_at: string | null;
}

export interface ConversationOut {
  id: string;
  chapter_id: string | null;
  kind: ConversationKind;
  title: string | null;
  protocol_version: number;
  created_at: string;
  members: ConversationMemberOut[] | null;
  // Presence/recency metadata only (board c344) — no ciphertext, no message count,
  // no unread indicator. has_messages drives the inbox preview text so the client
  // no longer has to fetch history per row just to know whether any exists.
  last_message_at: string | null;
  has_messages: boolean;
}

/** Ciphertext in — the server never parses ciphertext_b64 (SPEC §8.1). */
export interface MessageCreate {
  sender_device_id: string;
  ciphertext_b64: string;
  message_type?: MessageType;
}

export interface MessageOut {
  id: string;
  conversation_id: string;
  sender_device_id: string;
  ciphertext_b64: string;
  message_type: MessageType;
  created_at: string;
}

export interface MessageReceiptCreate {
  device_id: string;
  delivered_at?: string | null;
}

export interface MessageReceiptOut {
  message_id: string;
  device_id: string;
  delivered_at: string | null;
}

export async function createConversation(body: ConversationCreate): Promise<ConversationOut> {
  return request<ConversationOut>("/conversations", { method: "POST", body });
}

/** Cursor options for the inbox list, newest-first — same (before, before_id) shape
 * as ListMessagesOptions below. */
export interface ListConversationsOptions extends MessageRequestOptions {
  /** created_at cursor — conversations older than this. */
  before?: string;
  /** id tie-break for rows sharing the same created_at as `before`. */
  before_id?: string;
  limit?: number;
}

export async function listConversations(
  options: ListConversationsOptions = {},
): Promise<ConversationOut[]> {
  return request<ConversationOut[]>("/conversations", {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
    query: { before: options.before, before_id: options.before_id, limit: options.limit },
  });
}

/** One conversation's summary — added so a screen that only has a conversation id
 * (a deep link, or one reached after paging past the first inbox page) can still
 * resolve its title/kind without listConversations() being guaranteed to include it. */
export async function getConversation(conversationId: string, options: MessageRequestOptions = {}): Promise<ConversationOut> {
  return request<ConversationOut>(`/conversations/${conversationId}`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
  });
}

export async function sendMessage(
  conversationId: string,
  body: MessageCreate,
): Promise<MessageOut> {
  return request<MessageOut>(`/conversations/${conversationId}/messages`, {
    method: "POST",
    body,
  });
}

/** Cursor options for ciphertext history, newest-first. */
export interface ListMessagesOptions extends MessageRequestOptions {
  /** created_at cursor — messages older than this. */
  before?: string;
  /** id tie-break for rows sharing the same created_at as `before`. */
  before_id?: string;
  limit?: number;
}

/** Ciphertext history, newest-first pagination via the compound `(before, before_id)` cursor. */
export async function listMessages(
  conversationId: string,
  options: ListMessagesOptions = {},
): Promise<MessageOut[]> {
  return request<MessageOut[]>(`/conversations/${conversationId}/messages`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
    query: { before: options.before, before_id: options.before_id, limit: options.limit },
  });
}

/** Leave a conversation — server sets left_at; remaining clients rotate sender keys (SPEC §6.4). */
export async function leaveConversation(conversationId: string, options: MessageRequestOptions = {}): Promise<void> {
  return request<void>(`/conversations/${conversationId}/leave`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs, method: "POST",
  });
}

/** Live frames supply identifiers only; the server rechecks current visibility. */
export async function getMessagesById(conversationId: string, ids: readonly string[], options: MessageRequestOptions = {}): Promise<MessageOut[]> {
  const unique = [...new Set(ids.map(id => id.toLowerCase()))];
  if (ids.length > 50 || unique.length === 0 || unique.some(id => !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id))) {
    throw new Error("Invalid message selection.");
  }
  return request<MessageOut[]>(`/conversations/${conversationId}/messages/by-id`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
    query: { ids: unique.join(",") },
  });
}

export async function postReceipt(
  messageId: string,
  body: MessageReceiptCreate,
): Promise<MessageReceiptOut> {
  return request<MessageReceiptOut>(`/messages/${messageId}/receipts`, { method: "POST", body });
}

/** GET /users/search row (board c322): id, display name, avatar only — never email. */
export interface UserSearchResult {
  id: string;
  display_name: string;
  avatar_url: string | null;
}

/**
 * Search people the caller may message OFF their own chapter (board c322) — the wider
 * set `_require_reachable_off_chapter` already permits (a chapter mate, or anyone on
 * the caller's campus while the caller is campus-verified). Server-side minimum length,
 * cap and rate limit all apply; see routers/messages.py:search_users for the exact
 * rule and every exclusion (self, ghosts, suspended, blocked). Phone-number search is
 * explicitly out of scope (board c323).
 */
export async function searchUsers(query: string): Promise<UserSearchResult[]> {
  return request<UserSearchResult[]>("/users/search", { query: { q: query } });
}
