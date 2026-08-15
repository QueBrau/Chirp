/** Auth API: account bootstrap (POST /auth/bootstrap) + identity types mirroring backend schemas. */

import { request } from "./client";
import type { MembershipOut } from "./chapters";

export type AccountType = "greek" | "non_greek" | "alumni";

/** Mirrors backend UserCreate — firebase_uid comes from the verified identity, not the body. */
export interface UserCreate {
  email: string;
  display_name: string;
  avatar_url?: string | null;
  account_type: AccountType;
  campus_id?: string | null;
}

export interface UserOut {
  id: string;
  firebase_uid: string;
  email: string;
  display_name: string;
  avatar_url: string | null;
  account_type: AccountType;
  campus_id: string | null;
  is_ghost: boolean;
  created_at: string;
}

export interface CampusOut {
  id: string;
  name: string;
  slug: string;
}

/** Create the users row for an authenticated-but-unregistered Firebase identity. */
export async function bootstrap(body: UserCreate): Promise<UserOut> {
  return request<UserOut>("/auth/bootstrap", { method: "POST", body });
}

/** Resolve a campus id to its real name/slug (GET /campuses/{id}, c46). This is how
 * screens show the caller's actual campus instead of a hardcoded mock one. */
export async function getCampus(campusId: string): Promise<CampusOut> {
  return request<CampusOut>(`/campuses/${campusId}`);
}

/**
 * Current signed-in identity + chapter memberships. 404s with detail
 * "user_not_registered" when the Firebase user hasn't completed bootstrap()
 * yet — callers (SessionProvider) treat that as an "unregistered" state, not
 * an error.
 */
export async function fetchMe(): Promise<{ user: UserOut; memberships: MembershipOut[] }> {
  return request<{ user: UserOut; memberships: MembershipOut[] }>("/auth/me");
}
