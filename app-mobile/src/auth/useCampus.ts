import type { CampusOut } from "@/api/auth";

import { useSession } from "./SessionProvider";

/**
 * The signed-in user's campus. c67: this used to fetch GET /campuses/{id}
 * itself, per hook instance — with OrgsScreen and MemberOrgHub both mounted
 * on the Orgs tab, one screen mount fired two identical requests, plus a
 * third from Chirp. campus_id is a property of the session (derived from
 * user.campus_id), so SessionProvider now resolves it exactly once and this
 * is just a thin reader over that shared value — any number of call sites,
 * one fetch. Kept as its own export (rather than inlining useSession().campus
 * everywhere) so existing call sites don't need to change their imports.
 *
 * Fails soft to null on purpose, same as before: campus name is a cosmetic
 * label in every one of its call sites, so a failed lookup renders an absent
 * eyebrow rather than taking down the screen around it. Also null while
 * loading — callers cannot and should not distinguish "still fetching" from
 * "failed": both mean "do not render a name yet".
 */
export function useCampus(): CampusOut | null {
  return useSession().campus;
}
