/** Messaging API (ciphertext only): conversations, messages, receipts — routers/messages.py.
 *
 * The server never sees plaintext: ciphertext_b64 is an opaque base64 blob (SPEC §2.1).
 * Decrypted history lives ONLY in the on-device SQLite store (src/db/schema.ts).
 */

import { request } from "./client";

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

export async function listConversations(): Promise<ConversationOut[]> {
  return request<ConversationOut[]>("/conversations");
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
export interface ListMessagesOptions {
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
    query: { before: options.before, before_id: options.before_id, limit: options.limit },
  });
}

/** Leave a conversation — server sets left_at; remaining clients rotate sender keys (SPEC §6.4). */
export async function leaveConversation(conversationId: string): Promise<void> {
  return request<void>(`/conversations/${conversationId}/leave`, { method: "POST" });
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
