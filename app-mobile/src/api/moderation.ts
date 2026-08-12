/** Moderation API: content reports, user blocks, admin yak removal — routers/moderation.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_BLOCKS,
  MOCK_CURRENT_USER,
  MOCK_REPORTS,
  MOCK_YAKS,
  newMockId,
  nowIso,
} from "../mocks/data";

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
  if (USE_MOCKS) {
    const report: ContentReportOut = {
      id: newMockId("rpt"),
      reporter_id: MOCK_CURRENT_USER.id,
      target_type: body.target_type,
      target_id: body.target_id ?? null,
      forwarded_plaintext: body.forwarded_plaintext ?? null,
      reason: body.reason,
      status: "open",
      created_at: nowIso(),
    };
    MOCK_REPORTS.push(report);
    return mocked(report);
  }
  return request<ContentReportOut>("/moderation/reports", { method: "POST", body });
}

export async function listReports(): Promise<ContentReportOut[]> {
  if (USE_MOCKS) return mocked(MOCK_REPORTS);
  return request<ContentReportOut[]>("/moderation/reports");
}

export async function blockUser(blockedId: string): Promise<UserBlockOut> {
  if (USE_MOCKS) {
    const block: UserBlockOut = {
      blocker_id: MOCK_CURRENT_USER.id,
      blocked_id: blockedId,
      created_at: nowIso(),
    };
    MOCK_BLOCKS.push(block);
    return mocked(block);
  }
  return request<UserBlockOut>("/moderation/blocks", {
    method: "POST",
    body: { blocked_id: blockedId },
  });
}

export async function unblockUser(blockedId: string): Promise<void> {
  if (USE_MOCKS) {
    const index = MOCK_BLOCKS.findIndex(
      (b) => b.blocked_id === blockedId && b.blocker_id === MOCK_CURRENT_USER.id,
    );
    if (index >= 0) MOCK_BLOCKS.splice(index, 1);
    return mocked(undefined);
  }
  return request<void>("/moderation/blocks", { method: "DELETE", body: { blocked_id: blockedId } });
}

/** Admin removal of a yak (moderator action, distinct from author delete). */
export async function removeYak(yakId: string, reason: string): Promise<void> {
  if (USE_MOCKS) {
    const index = MOCK_YAKS.findIndex((y) => y.id === yakId);
    if (index >= 0) MOCK_YAKS.splice(index, 1);
    return mocked(undefined);
  }
  return request<void>(`/moderation/yaks/${yakId}/remove`, { method: "POST", body: { reason } });
}
