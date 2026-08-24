/** Polls API: open a vote, cast a ballot, read the tally — routers/polls.py (c162). */

import { request } from "./client";

export type PollStatus = "open" | "closed";

export interface PollCreate {
  question: string;
  options: string[];
  meeting_id?: string | null;
}

/**
 * One option and how many votes it holds. Aggregate only — the server never
 * reports WHO voted, and no shape in this file has room for a voter. Ballot
 * secrecy is a product guarantee (see backend models/polls.py), not a view
 * detail, so do not add a voter list here without a card that says why.
 */
export interface PollOptionResult {
  id: string;
  text: string;
  position: number;
  votes: number;
}

export interface PollOut {
  id: string;
  chapter_id: string;
  meeting_id: string | null;
  question: string;
  status: PollStatus;
  created_by: string;
  created_at: string;
  closed_at: string | null;
  options: PollOptionResult[];
  total_votes: number;
  /** What the CALLER picked, or null. Never describes anybody else. */
  my_option_id: string | null;
}

export async function listPolls(chapterId: string, meetingId?: string): Promise<PollOut[]> {
  return request<PollOut[]>(`/chapters/${chapterId}/polls`, {
    query: { meeting_id: meetingId },
  });
}

export async function createPoll(chapterId: string, body: PollCreate): Promise<PollOut> {
  return request<PollOut>(`/chapters/${chapterId}/polls`, { method: "POST", body });
}

/** Cast or CHANGE the caller's vote. Changing replaces; it never adds a second ballot. */
export async function castVote(
  chapterId: string,
  pollId: string,
  optionId: string,
): Promise<PollOut> {
  return request<PollOut>(`/chapters/${chapterId}/polls/${pollId}/vote`, {
    method: "POST",
    body: { option_id: optionId },
  });
}

export async function closePoll(chapterId: string, pollId: string): Promise<PollOut> {
  return request<PollOut>(`/chapters/${chapterId}/polls/${pollId}/close`, { method: "POST" });
}

export async function deletePoll(chapterId: string, pollId: string): Promise<void> {
  await request<void>(`/chapters/${chapterId}/polls/${pollId}`, { method: "DELETE" });
}
