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
  // No campus_id: the server owns it (board c85). It was never sent from here
  // anyway — account-type.tsx passes email, display_name and account_type — but
  // leaving it in the type invites someone to start sending it.
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
  // c126: non-null means suspended (since this timestamp) — never auto-clears
  // with time, only a moderator restores it to null. Reachable here because
  // GET /auth/me is (still, as of c126) the one authenticated route that is NOT
  // suspension-gated; every other route 403s a suspended caller instead of
  // reaching a response body at all. See schemas/identity.py's UserOut comment.
  suspended_at: string | null;
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

/**
 * Mirrors backend CampusVerificationStatus (c86).
 *
 * `verified` already accounts for the yearly re-check, so it is the ONLY field a
 * gate should branch on. `verified_at` is returned even when `verified` is false
 * on purpose: it is how a LAPSED verification is told apart from one that never
 * happened, which are two different screens — "your verification expired" versus
 * "verify your .edu". A returning student should not be told they have never
 * been here.
 */
export interface CampusVerificationStatus {
  verified: boolean;
  verified_at: string | null;
  campus_id: string | null;
}

/**
 * The caller's .edu verification state.
 *
 * WHY THIS EXISTS RATHER THAN A campus_id CHECK (c110): since c88 the campus feed
 * and Chirp are gated on a verification timestamp, not on having a campus. A user
 * who joined by chapter invite HAS a campus_id and is still refused, so branching
 * on `user.campus_id !== null` sends them down the call-the-endpoint path into a
 * 403 — which is exactly the mistake the server was just moved off.
 */
export async function getCampusVerification(): Promise<CampusVerificationStatus> {
  return request<CampusVerificationStatus>("/auth/campus-verification");
}

/** Request a one-time code at an .edu address. 202 — accepted for delivery. */
export async function startCampusVerification(eduEmail: string): Promise<void> {
  await request("/auth/campus-verification", { method: "POST", body: { edu_email: eduEmail } });
}

/** Redeem a code; on success the campus feed and Chirp open. */
export async function redeemCampusVerification(code: string): Promise<CampusVerificationStatus> {
  return request<CampusVerificationStatus>("/auth/campus-verification/redeem", {
    method: "POST",
    body: { code },
  });
}
