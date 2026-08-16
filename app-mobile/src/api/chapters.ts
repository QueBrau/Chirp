/** Chapters API: CRUD, member management, invites, join-by-code — mirrors routers/chapters.py. */

import { request } from "./client";

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
export interface RoleMetaOut {
  roles: RoleName[];
  eboard: RoleName[];
  invitable: RoleName[];
}

export async function listChapters(): Promise<ChapterOut[]> {
  return request<ChapterOut[]>("/chapters");
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
