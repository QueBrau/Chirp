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
  /** Daily-rotating pseudonym, e.g. "Quiet-Magnolia-07" (c390). Server-derived; see the file header. */
  author_label: string;
}

export type ChirpVoteValue = -1 | 1;

export interface ChirpVoteOut {
  chirp_id: string;
  value: number;
}

/**
 * GET /campuses/{campus_id}/chirps response shape: ChirpOut plus the caller's OWN
 * vote only (backend routers/chirps.py `ChirpFeedOut`).
 *
 * c390: `author_label` (on ChirpOut) is the ONE author-derived field on the wire —
 * a daily-rotating pseudonym, stable for one author within a UTC day on one campus
 * and unrelated the next day. It is derived server-side from a stored random seed,
 * so it is a label and not the author id in disguise; there is still no author
 * IDENTIFIER of any kind here, and nothing the client can resolve back to a person.
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
