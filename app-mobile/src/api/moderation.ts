/** Moderation API: content reports, user blocks, admin yak removal — routers/moderation.py. */

import { request } from "./client";

export type ReportTargetType = "yak" | "post" | "comment" | "message_forward" | "user";
export type ReportStatus = "open" | "actioned" | "dismissed";

/** For E2EE message reports the client forwards plaintext (SPEC §6.7). */
export interface ContentReportCreate {
  target_type: ReportTargetType;
  target_id?: string | null;
  forwarded_plaintext?: string | null;
  reason: string;
}

export interface ContentReportOut {
  id: string;
  reporter_id: string;
  target_type: ReportTargetType;
  target_id: string | null;
  forwarded_plaintext: string | null;
  reason: string;
  status: ReportStatus;
  created_at: string;
}

export interface UserBlockOut {
  blocker_id: string;
  blocked_id: string;
  created_at: string;
}

export async function createReport(body: ContentReportCreate): Promise<ContentReportOut> {
  return request<ContentReportOut>("/moderation/reports", { method: "POST", body });
}

export async function listReports(): Promise<ContentReportOut[]> {
  return request<ContentReportOut[]>("/moderation/reports");
}

export async function blockUser(blockedId: string): Promise<UserBlockOut> {
  return request<UserBlockOut>("/moderation/blocks", {
    method: "POST",
    body: { blocked_id: blockedId },
  });
}

export async function unblockUser(blockedId: string): Promise<void> {
  // backend/app/routers/moderation.py `delete_block` takes blocked_id as a QUERY
  // param ("identified by ?blocked_id=", a bare uuid.UUID arg outside the path) —
  // NOT a request body, so it rides `query` here rather than `body`.
  return request<void>("/moderation/blocks", { method: "DELETE", query: { blocked_id: blockedId } });
}

/**
 * Block the (server-known) author of a yak — distinct from blockUser because a
 * yak carries NO author field on the wire (SPEC §8.3): the client never learns
 * whose yak it is, so it can't pass a blocked_id to POST /moderation/blocks.
 * The server resolves the yak's author internally and blocks them; the response
 * identifies nothing back to the client (204, no body).
 */
export async function blockYakAuthor(yakId: string): Promise<void> {
  return request<void>(`/moderation/blocks/by-yak/${yakId}`, { method: "POST" });
}

/** Admin removal of a yak (moderator action, distinct from author delete). */
export async function removeYak(yakId: string, reason: string): Promise<void> {
  return request<void>(`/moderation/yaks/${yakId}/remove`, { method: "POST", body: { reason } });
}
