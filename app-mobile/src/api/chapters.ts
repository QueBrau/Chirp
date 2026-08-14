/** Chapters API: CRUD, member management, invites, join-by-code — mirrors routers/chapters.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_CHAPTER,
  MOCK_CURRENT_MEMBERSHIP,
  MOCK_INVITES,
  MOCK_MEMBERSHIPS,
  MOCK_ROLE_META,
  mockUserById,
} from "../mocks/data";

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
}

/** listMembers() row: MembershipOut plus the joined display name/photo (GET /chapters/{id}/members). */
export interface MemberOut extends MembershipOut {
  display_name: string;
  avatar_url: string | null;
}

export interface ChapterInviteCreate {
  role?: RoleName;
  expires_at?: string | null;
}

export interface ChapterInviteOut {
  id: string;
  chapter_id: string;
  code: string;
  role: RoleName;
  expires_at: string | null;
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
  if (USE_MOCKS) return mocked([MOCK_CHAPTER]);
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
  if (USE_MOCKS) {
    return mocked([{ ...MOCK_CURRENT_MEMBERSHIP, chapter_name: MOCK_CHAPTER.chapter_name }]);
  }
  return request<MyMembershipOut[]>("/me/memberships");
}

export async function createChapter(body: ChapterCreate): Promise<ChapterOut> {
  if (USE_MOCKS) return mocked(MOCK_CHAPTER);
  return request<ChapterOut>("/chapters", { method: "POST", body });
}

export async function getChapter(chapterId: string): Promise<ChapterOut> {
  if (USE_MOCKS) return mocked(MOCK_CHAPTER);
  return request<ChapterOut>(`/chapters/${chapterId}`);
}

export async function updateChapter(chapterId: string, body: ChapterUpdate): Promise<ChapterOut> {
  if (USE_MOCKS) return mocked({ ...MOCK_CHAPTER, ...body } as ChapterOut);
  return request<ChapterOut>(`/chapters/${chapterId}`, { method: "PATCH", body });
}

export async function listMembers(chapterId: string): Promise<MemberOut[]> {
  if (USE_MOCKS) {
    return mocked(
      MOCK_MEMBERSHIPS.map((membership) => {
        const user = mockUserById(membership.user_id);
        return { ...membership, display_name: user?.display_name ?? "", avatar_url: user?.avatar_url ?? null };
      }),
    );
  }
  return request<MemberOut[]>(`/chapters/${chapterId}/members`);
}

export async function getRoleMeta(chapterId: string): Promise<RoleMetaOut> {
  if (USE_MOCKS) return mocked(MOCK_ROLE_META);
  return request<RoleMetaOut>(`/chapters/${chapterId}/role-meta`);
}

export async function updateMember(
  chapterId: string,
  body: MembershipUpdate,
): Promise<MembershipOut> {
  if (USE_MOCKS) {
    const membership = MOCK_MEMBERSHIPS.find((m) => m.user_id === body.user_id);
    if (membership) {
      if (body.role != null) membership.role = body.role;
      if (body.status != null) membership.status = body.status;
      if (body.pledge_class !== undefined) membership.pledge_class = body.pledge_class;
      return mocked(membership);
    }
    return mocked(MOCK_CURRENT_MEMBERSHIP);
  }
  return request<MembershipOut>(`/chapters/${chapterId}/members`, { method: "PATCH", body });
}

export async function createInvite(
  chapterId: string,
  body: ChapterInviteCreate,
): Promise<ChapterInviteOut> {
  if (USE_MOCKS) return mocked(MOCK_INVITES[0]);
  return request<ChapterInviteOut>(`/chapters/${chapterId}/invites`, { method: "POST", body });
}

/** Redeem an invite code (deep link `chirp://join-chapter?code=...`). */
export async function joinChapter(code: string): Promise<MembershipOut> {
  if (USE_MOCKS) return mocked(MOCK_CURRENT_MEMBERSHIP);
  return request<MembershipOut>("/chapters/join", { method: "POST", body: { code } });
}
