/** Chirp API: anonymous campus board + voting — routers/chirps.py.
 *
 * ChirpOut has NO author field of any kind (SPEC §8.3): anonymous to peers,
 * pseudonymous to the server only for moderation.
 */

import { request } from "./client";

export interface ChirpCreate {
  body: string;
}

/** Anonymous to peers: NO author field of any kind (SPEC §8.3). */
export interface ChirpOut {
  id: string;
  campus_id: string;
  body: string;
  score: number;
  created_at: string;
}

export type ChirpVoteValue = -1 | 1;

export interface ChirpVoteOut {
  chirp_id: string;
  value: number;
}

/**
 * GET /campuses/{campus_id}/chirps response shape: ChirpOut plus the caller's OWN
 * vote only (backend routers/chirps.py `ChirpFeedOut`) — still no author field of
 * any kind (SPEC §8.3).
 */
export interface ChirpFeedOut extends ChirpOut {
  my_vote: number | null;
}

export interface ListChirpsOptions {
  limit?: number;
  /** created_at cursor — chirps older than this. */
  before?: string;
  before_id?: string;
}

export async function listChirps(campusId: string, opts: ListChirpsOptions = {}): Promise<ChirpFeedOut[]> {
  return request<ChirpFeedOut[]>(`/campuses/${campusId}/chirps`, {
    query: { limit: opts.limit, before: opts.before, before_id: opts.before_id },
  });
}

export async function createChirp(campusId: string, body: ChirpCreate): Promise<ChirpOut> {
  return request<ChirpOut>(`/campuses/${campusId}/chirps`, { method: "POST", body });
}

/** Upsert the caller's vote (PUT is idempotent per user). */
export async function voteChirp(chirpId: string, value: ChirpVoteValue): Promise<ChirpVoteOut> {
  return request<ChirpVoteOut>(`/chirps/${chirpId}/vote`, { method: "PUT", body: { value } });
}

/** Author-only delete of one's own chirp. */
export async function deleteChirp(chirpId: string): Promise<void> {
  return request<void>(`/chirps/${chirpId}`, { method: "DELETE" });
}
