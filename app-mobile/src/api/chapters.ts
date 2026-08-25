/** Chapters API: CRUD, member management, invites, join-by-code — mirrors routers/chapters.py. */

import { request } from "./client";
import type { AttendanceWindow } from "./meetings";

export type RoleName =
  | "president"
  | "vice_president"
  | "treasurer"
  | "secretary"
  | "historian"
  | "member"
  | "pledge"
  | "alumni";

export type MembershipStatus = "active" | "inactive" | "removed";

export interface ChapterCreate {
  campus_id: string;
  org_name: string;
  chapter_name?: string | null;
}

export interface ChapterUpdate {
  org_name?: string | null;
  chapter_name?: string | null;
}

export interface ChapterOut {
  id: string;
  campus_id: string;
  org_name: string;
  chapter_name: string | null;
  stripe_account_id: string | null;
  created_at: string;
}

/** Mirrors backend MembershipUpdate — targets one member by user_id. */
export interface MembershipUpdate {
  user_id: string;
  role?: RoleName | null;
  status?: MembershipStatus | null;
  pledge_class?: string | null;
}

export interface MembershipOut {
  id: string;
  user_id: string;
  chapter_id: string;
  role: RoleName;
  status: MembershipStatus;
  pledge_class: string | null;
  joined_at: string;
  /** Joined from chapters by GET /me/memberships only — null on the /auth/me path. */
  org_name?: string | null;
  chapter_name?: string | null;
}

/** listMembers() row: MembershipOut plus the joined display name/photo (GET /chapters/{id}/members). */
export interface MemberOut extends MembershipOut {
  display_name: string;
  avatar_url: string | null;
}

export interface ChapterInviteCreate {
  role?: RoleName;
  /** Omit for the server default. There is no value meaning "never" (c105). */
  expires_at?: string | null;
  /** Redemptions the code is good for; server default 25, hard cap 200 (c105). */
  max_uses?: number;
}

export interface ChapterInviteOut {
  id: string;
  chapter_id: string;
  code: string;
  role: RoleName;
  /** Never null since c105 — every code expires. */
  expires_at: string;
  max_uses: number;
  uses: number;
  revoked_at: string | null;
  created_by: string;
}

/** MembershipOut plus the chapter's display name, so role-gated screens (treasurer/
 * secretary dashboards) can resolve their real chapter_id from `GET /me/memberships`
 * instead of importing MOCK_CURRENT_MEMBERSHIP directly. */
export interface MyMembershipOut extends MembershipOut {
  chapter_name: string | null;
}

/** GET /chapters/{id}/role-meta — role taxonomy served from backend permissions.py
 * (c44), so this app never hand-mirrors the eboard set or the create_invite rule.
 * `invitable` is what the CALLER may mint (empty for non-eboard), common roles first. */
/**
 * What the server says about roles, so the app never keeps its own copy (c44, c80).
 *
 * `capabilities` is what THIS caller may DO, by name. Gate UI on it — ask "may I see
 * the dues tile", never "am I a treasurer or a president". The names come from
 * permissions.CAPABILITIES, which is the same definition the routers gate on, so a
 * permission change moves the server and the UI together. Deriving a capability from
 * `roles` client-side rebuilds exactly the drift these cards exist to delete.
 */
export type Capability =
  | "dues_admin"
  | "minutes_admin"
  | "members_admin"
  | "moderation"
  | "lineage_admin"
  | "deputy_overview";

export interface RoleMetaOut {
  roles: RoleName[];
  eboard: RoleName[];
  invitable: RoleName[];
  capabilities: Capability[];
}

/**
 * Caller's own active memberships — GET /me/memberships. This is how role-gated
 * screens learn their real chapter_id (and role) instead of importing
 * MOCK_CURRENT_MEMBERSHIP directly. Distinct from fetchMe()'s embedded memberships
 * (src/api/auth.ts / SessionProvider): that path doesn't join chapter_name, which
 * the treasurer/secretary CSV export filenames need.
 */
export async function myMemberships(): Promise<MyMembershipOut[]> {
  return request<MyMembershipOut[]>("/me/memberships");
}

export async function createChapter(body: ChapterCreate): Promise<ChapterOut> {
  return request<ChapterOut>("/chapters", { method: "POST", body });
}

export async function getChapter(chapterId: string): Promise<ChapterOut> {
  return request<ChapterOut>(`/chapters/${chapterId}`);
}

export async function updateChapter(chapterId: string, body: ChapterUpdate): Promise<ChapterOut> {
  return request<ChapterOut>(`/chapters/${chapterId}`, { method: "PATCH", body });
}

export async function listMembers(chapterId: string): Promise<MemberOut[]> {
  return request<MemberOut[]>(`/chapters/${chapterId}/members`);
}

export async function getRoleMeta(chapterId: string): Promise<RoleMetaOut> {
  return request<RoleMetaOut>(`/chapters/${chapterId}/role-meta`);
}

/** One dated span of a membership holding a role — mirrors backend RoleTermOut
 * (board card c83: a chapter role is a DATED TERM, not a plain fact). Rows come
 * back newest first; `ended_at` null marks the OPEN term, the role the member
 * holds right now. `changed_by` is null on rows nobody's PATCH actually created
 * (the 0021 backfill and the seed a brand-new membership gets) — see c180's
 * client-side date rule at its call site in chapter/member/[id].tsx. */
export interface RoleTerm {
  id: string;
  membership_id: string;
  role: RoleName;
  started_at: string;
  ended_at: string | null;
  changed_by: string | null;
}

/** A member's role history, newest first (board card c83/c180) — GET
 * /chapters/{id}/members/{userId}/role-terms. Gated the same as the roster
 * itself: any active member of the chapter may read it. */
export async function getRoleTerms(chapterId: string, userId: string): Promise<RoleTerm[]> {
  return request<RoleTerm[]>(`/chapters/${chapterId}/members/${userId}/role-terms`);
}

export async function updateMember(
  chapterId: string,
  body: MembershipUpdate,
): Promise<MembershipOut> {
  return request<MembershipOut>(`/chapters/${chapterId}/members`, { method: "PATCH", body });
}

export async function createInvite(
  chapterId: string,
  body: ChapterInviteCreate,
): Promise<ChapterInviteOut> {
  return request<ChapterInviteOut>(`/chapters/${chapterId}/invites`, { method: "POST", body });
}

/** Every code this chapter has minted, live and dead (c111). E-board only.
 *  Dead ones are included on purpose: "is the code going around still live" is
 *  the question the screen exists to answer. */
export async function listInvites(chapterId: string): Promise<ChapterInviteOut[]> {
  return request<ChapterInviteOut[]>(`/chapters/${chapterId}/invites`);
}

/** Kill a leaked code (c105). By code, not id — the string is what leaks. */
export async function revokeInvite(chapterId: string, code: string): Promise<ChapterInviteOut> {
  return request<ChapterInviteOut>(`/chapters/${chapterId}/invites/revoke`, {
    method: "POST",
    body: { code },
  });
}

/** Redeem an invite code (deep link `chirp://join-chapter?code=...`). */
export async function joinChapter(code: string): Promise<MembershipOut> {
  return request<MembershipOut>("/chapters/join", { method: "POST", body: { code } });
}

// ---- president overview (board card c171) ----

/** How many ACTIVE members hold one role. Roles nobody holds are omitted. */
export interface RoleCount {
  role: RoleName;
  count: number;
}

export interface RosterOverview {
  active: number;
  inactive: number;
  /** Active members only, so these sum to `active` and never to active + inactive. */
  by_role: RoleCount[];
}

/**
 * The current dues cycle and how far through collecting it the chapter is.
 * Every field is null/zero before a chapter has ever opened a cycle.
 *
 * `paid_members + outstanding_members === roster.active`, always — both are spined on
 * the current active roster. `collected_cents` deliberately is NOT: it counts money
 * that came in, including from members who have since gone inactive.
 */
export interface DuesOverview {
  cycle_id: string | null;
  cycle_name: string | null;
  amount_cents: number | null;
  due_date: string | null;
  paid_members: number;
  outstanding_members: number;
  collected_cents: number;
}

/** Active members with at least one recorded ABSENT in the window. Excused is not an absence. */
export interface AttendanceOverview {
  meetings_in_window: number;
  members_with_absence: number;
  window_start: string | null;
  window_end: string | null;
}

export interface LineageOverview {
  unconfirmed_edges: number;
}

/** Codes that could still be redeemed: not revoked, not expired, uses < max_uses (c105). */
export interface InviteOverview {
  live_codes: number;
  remaining_uses: number;
}

/**
 * One request's worth of chapter health for the President dashboard.
 *
 * Chapter-scoped throughout. There is deliberately no moderation count: content
 * reports are scoped by CAMPUS, not chapter, so a number here would be campus data
 * wearing a chapter label. The Moderation tile owns that question.
 */
export interface ChapterOverview {
  chapter_id: string;
  generated_at: string;
  roster: RosterOverview;
  dues: DuesOverview;
  attendance: AttendanceOverview;
  lineage: LineageOverview;
  invites: InviteOverview;
}

/**
 * Chapter health in one call; president only (board card c171).
 *
 * Replaces walking into the Treasurer, Secretary and Historian screens one at a time
 * to answer "are dues collected" and "is anyone failing attendance". Pass the same
 * window the Secretary dashboard computes so the meeting counts on the two screens
 * cannot disagree; omit it for all time.
 */
export async function getChapterOverview(
  chapterId: string,
  window: AttendanceWindow = {},
): Promise<ChapterOverview> {
  return request<ChapterOverview>(`/chapters/${chapterId}/overview`, {
    query: { start: window.start, end: window.end },
  });
}

// ---- deputy overview, Vice President dashboard (board card c163) ----

/**
 * Roster, dues status, and open invites — the READ-ONLY subset of ChapterOverview
 * the Vice President's stand-in "Deputy President" dashboard shows (Jose's product
 * ruling: a read view of president-admin data, delegation explicitly out of the
 * alpha). No `attendance` or `lineage` fields exist on this type because the server
 * never sends them here (GET /chapters/{id}/deputy-overview) — the VP holds neither
 * minutes_admin nor lineage_admin, so those sections are absent from the response
 * itself, not merely hidden by this screen.
 */
export interface DeputyOverview {
  chapter_id: string;
  generated_at: string;
  roster: RosterOverview;
  dues: DuesOverview;
  invites: InviteOverview;
}

/**
 * Deputy President dashboard's one call; vice_president or president only, gated on
 * the deputy_overview capability (c163). Mirrors getChapterOverview's shape without
 * the two sections the VP's capability set doesn't cover.
 */
export async function getDeputyOverview(chapterId: string): Promise<DeputyOverview> {
  return request<DeputyOverview>(`/chapters/${chapterId}/deputy-overview`);
}
