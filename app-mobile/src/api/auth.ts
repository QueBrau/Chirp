/** Auth API: account bootstrap (POST /auth/bootstrap) + identity types mirroring backend schemas. */

import { mocked, request, USE_MOCKS } from "./client";
import { MOCK_CURRENT_MEMBERSHIP, MOCK_CURRENT_USER } from "../mocks/data";
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
  if (USE_MOCKS) return mocked(MOCK_CURRENT_USER);
  return request<UserOut>("/auth/bootstrap", { method: "POST", body });
}

/**
 * Current signed-in identity + chapter memberships. 404s with detail
 * "user_not_registered" when the Firebase user hasn't completed bootstrap()
 * yet — callers (SessionProvider) treat that as an "unregistered" state, not
 * an error.
 */
export async function fetchMe(): Promise<{ user: UserOut; memberships: MembershipOut[] }> {
  if (USE_MOCKS) return mocked({ user: MOCK_CURRENT_USER, memberships: [MOCK_CURRENT_MEMBERSHIP] });
  return request<{ user: UserOut; memberships: MembershipOut[] }>("/auth/me");
}
