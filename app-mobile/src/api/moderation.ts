/** Moderation API: content reports, user blocks, admin chirp removal — routers/moderation.py. */

import { request } from "./client";

export type ReportTargetType = "chirp" | "post" | "comment" | "message_forward" | "user";
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

/** One page of reports. `status` filters SERVER-SIDE, which matters: filtering after
 * paging would let a page of resolved reports render an empty queue while open ones
 * sat on later pages (c258). */
export interface ReportPageQuery {
  status?: "open" | "actioned" | "dismissed";
  before?: string;
  beforeId?: string;
  limit?: number;
}

export async function listReports(
  options: ReportPageQuery = {},
): Promise<ContentReportOut[]> {
  const paired = options.before !== undefined && options.beforeId !== undefined;
  return request<ContentReportOut[]>("/moderation/reports", {
    query: {
      status: options.status,
      before: paired ? options.before : undefined,
      before_id: paired ? options.beforeId : undefined,
      limit: options.limit,
    },
  });
}

export type ReportResolution = "actioned" | "dismissed";

/**
 * Close a report as actioned or dismissed (c78, using c91's route — it shipped with
 * no client function or call site at all, same shape as c77's chapter-identity gap
 * in reverse). `reason` is required server-side for the moderation_actions audit
 * row, same as every other route in this file.
 */
export async function resolveReport(
  reportId: string,
  status: ReportResolution,
  reason: string,
): Promise<ContentReportOut> {
  return request<ContentReportOut>(`/moderation/reports/${reportId}`, {
    method: "PATCH",
    body: { status, reason },
  });
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
 * Block the (server-known) author of a chirp — distinct from blockUser because a
 * chirp carries NO author field on the wire (SPEC §8.3): the client never learns
 * whose chirp it is, so it can't pass a blocked_id to POST /moderation/blocks.
 * The server resolves the chirp's author internally and blocks them; the response
 * identifies nothing back to the client (204, no body).
 */
export async function blockChirpAuthor(chirpId: string): Promise<void> {
  return request<void>(`/moderation/blocks/by-chirp/${chirpId}`, { method: "POST" });
}

/** Admin removal of a chirp (moderator action, distinct from author delete). */
export async function removeChirp(chirpId: string, reason: string): Promise<void> {
  return request<void>(`/moderation/chirps/${chirpId}/remove`, { method: "POST", body: { reason } });
}
