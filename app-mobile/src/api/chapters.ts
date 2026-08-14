/** Chapters API: CRUD, member management, invites, join-by-code — mirrors routers/chapters.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_CHAPTER,
  MOCK_CURRENT_MEMBERSHIP,
  MOCK_INVITES,
  MOCK_MEMBERSHIPS,
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

export async function listMembers(chapterId: string): Promise<MembershipOut[]> {
  if (USE_MOCKS) return mocked(MOCK_MEMBERSHIPS);
  return request<MembershipOut[]>(`/chapters/${chapterId}/members`);
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
