/** Touse/Bouse weekly house leaderboard — mirrors routers/house.py (board card c175). */

import { request } from "./client";

export interface HouseBallot {
  campus_id: string;
  week_start: string;
  touse_chapter_id: string;
  bouse_chapter_id: string | null;
  updated_at: string;
}

export interface HouseStanding {
  chapter_id: string;
  org_name: string;
  chapter_name: string | null;
  rank: number;
  touse_votes: number;
  bouse_votes: number;
  net: number;
}

/**
 * A house that has not cleared the vote threshold this week.
 *
 * Kept separate from the ranking rather than sorted to the bottom, and the screen must
 * keep it that way: "came last" and "three people voted" are different statements about
 * a real organisation, and the bottom of this leaderboard is public.
 */
export interface UnrankedHouse {
  chapter_id: string;
  org_name: string;
  chapter_name: string | null;
  votes: number;
}

export interface HouseLeaderboard {
  campus_id: string;
  week_start: string;
  /** The sample every number here is drawn from. Always shown, never implied. */
  ballots_cast: number;
  min_votes_to_rank: number;
  ranked: HouseStanding[];
  unranked: UnrankedHouse[];
  my_ballot: HouseBallot | null;
}

export interface TermStanding {
  chapter_id: string;
  org_name: string;
  chapter_name: string | null;
  weekly_wins: number;
  net: number;
}

export interface TermTitleRace {
  campus_id: string;
  /** "Fall 26" — what gets engraved on the title. */
  term_label: string;
  term_start: string;
  term_end: string;
  weeks_scored: number;
  /** null until a week has actually been won. */
  leader: TermStanding | null;
  standings: TermStanding[];
}

/**
 * Cast or change this week's ballot.
 *
 * PUT because it is idempotent per week by construction: the server keys on
 * (campus, week, voter), so calling this again REPLACES your vote rather than adding
 * one. There is deliberately no week parameter — the server decides which week it is,
 * so a finished week cannot be voted into after the fact.
 */
export async function castHouseBallot(
  campusId: string,
  touseChapterId: string,
  bouseChapterId?: string | null,
): Promise<HouseBallot> {
  return request<HouseBallot>(`/campuses/${campusId}/house-ballot`, {
    method: "PUT",
    body: { touse_chapter_id: touseChapterId, bouse_chapter_id: bouseChapterId ?? null },
  });
}

/** This week's ranking, or a past week's by `weekStart` (read-only addressing). */
export async function getHouseLeaderboard(
  campusId: string,
  weekStart?: string,
): Promise<HouseLeaderboard> {
  return request<HouseLeaderboard>(`/campuses/${campusId}/house-leaderboard`, {
    query: { week_start: weekStart },
  });
}

/** The race for the term title — "Touse of Fall 26". */
export async function getHouseTitleRace(campusId: string): Promise<TermTitleRace> {
  return request<TermTitleRace>(`/campuses/${campusId}/house-title-race`);
}
