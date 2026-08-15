/** Yak API: anonymous campus board + voting — routers/yaks.py.
 *
 * YakOut has NO author field of any kind (SPEC §8.3): anonymous to peers,
 * pseudonymous to the server only for moderation.
 */

import { request } from "./client";

export interface YakCreate {
  body: string;
}

/** Anonymous to peers: NO author field of any kind (SPEC §8.3). */
export interface YakOut {
  id: string;
  campus_id: string;
  body: string;
  score: number;
  created_at: string;
}

export type YakVoteValue = -1 | 1;

export interface YakVoteOut {
  yak_id: string;
  value: number;
}

/**
 * GET /campuses/{campus_id}/yaks response shape: YakOut plus the caller's OWN
 * vote only (backend routers/yaks.py `YakFeedOut`) — still no author field of
 * any kind (SPEC §8.3).
 */
export interface YakFeedOut extends YakOut {
  my_vote: number | null;
}

export async function listYaks(campusId: string): Promise<YakFeedOut[]> {
  return request<YakFeedOut[]>(`/campuses/${campusId}/yaks`);
}

export async function createYak(campusId: string, body: YakCreate): Promise<YakOut> {
  return request<YakOut>(`/campuses/${campusId}/yaks`, { method: "POST", body });
}

/** Upsert the caller's vote (PUT is idempotent per user). */
export async function voteYak(yakId: string, value: YakVoteValue): Promise<YakVoteOut> {
  return request<YakVoteOut>(`/yaks/${yakId}/vote`, { method: "PUT", body: { value } });
}

/** Author-only delete of one's own yak. */
export async function deleteYak(yakId: string): Promise<void> {
  return request<void>(`/yaks/${yakId}`, { method: "DELETE" });
}
