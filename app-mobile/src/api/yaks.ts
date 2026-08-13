/** Yak API: anonymous campus board + voting — routers/yaks.py.
 *
 * YakOut has NO author field of any kind (SPEC §8.3): anonymous to peers,
 * pseudonymous to the server only for moderation.
 */

import { mocked, request, USE_MOCKS } from "./client";
import { MOCK_MY_YAK_VOTES, MOCK_YAKS, newMockId, nowIso } from "../mocks/data";

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
  if (USE_MOCKS) {
    return mocked(
      MOCK_YAKS.filter((y) => y.campus_id === campusId)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .map((y) => ({ ...y, my_vote: MOCK_MY_YAK_VOTES[y.id] ?? null })),
    );
  }
  return request<YakFeedOut[]>(`/campuses/${campusId}/yaks`);
}

export async function createYak(campusId: string, body: YakCreate): Promise<YakOut> {
  if (USE_MOCKS) {
    const yak: YakOut = {
      id: newMockId("yak"),
      campus_id: campusId,
      body: body.body,
      score: 0,
      created_at: nowIso(),
    };
    MOCK_YAKS.push(yak);
    return mocked(yak);
  }
  return request<YakOut>(`/campuses/${campusId}/yaks`, { method: "POST", body });
}

/** Upsert the caller's vote (PUT is idempotent per user). */
export async function voteYak(yakId: string, value: YakVoteValue): Promise<YakVoteOut> {
  if (USE_MOCKS) {
    const yak = MOCK_YAKS.find((y) => y.id === yakId);
    const previous = MOCK_MY_YAK_VOTES[yakId] ?? 0;
    if (yak) yak.score += value - previous;
    MOCK_MY_YAK_VOTES[yakId] = value;
    return mocked({ yak_id: yakId, value });
  }
  return request<YakVoteOut>(`/yaks/${yakId}/vote`, { method: "PUT", body: { value } });
}

/** Author-only delete of one's own yak. */
export async function deleteYak(yakId: string): Promise<void> {
  if (USE_MOCKS) {
    const index = MOCK_YAKS.findIndex((y) => y.id === yakId);
    if (index >= 0) MOCK_YAKS.splice(index, 1);
    return mocked(undefined);
  }
  return request<void>(`/yaks/${yakId}`, { method: "DELETE" });
}
