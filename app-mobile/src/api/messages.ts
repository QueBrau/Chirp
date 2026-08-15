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

/** Ciphertext history, newest-first pagination via `before` (ISO timestamp) + `limit`. */
export async function listMessages(
  conversationId: string,
  options: { before?: string; limit?: number } = {},
): Promise<MessageOut[]> {
  return request<MessageOut[]>(`/conversations/${conversationId}/messages`, {
    query: { before: options.before, limit: options.limit },
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
