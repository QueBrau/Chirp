/**
 * Roster identity lookups (board card c238).
 *
 * One definition rather than the three verbatim copies this replaced - chapter/index
 * .tsx, chapter/member/[id].tsx and chapter/event/[id].tsx each declared it, and each
 * carried its own half of the comment below.
 *
 * Kept out of roleTerms.ts on purpose: that file is about how a role is LABELLED and
 * when a role term's dates may honestly be shown. This is about finding a person, and
 * the two would only be neighbours because both happen to take a roster.
 *
 * The inline `.find(...)` calls in chapter/dues-plans.tsx and chapter/secretary.tsx are
 * deliberately NOT routed through here: they resolve a member on a different path and
 * fall back their own way, so sharing this would change what they do, not just where
 * they say it.
 */

import type { MemberOut } from "@/api/chapters";

/** Resolve a user id to their roster row. There is no GET /users/{id} - the
 * member list of the caller's own chapter is the only name/photo source, so a
 * host or guest who has since left the roster falls back to "Unknown"/"Guest". */
export function findMember(members: MemberOut[], userId: string): MemberOut | undefined {
  return members.find((member) => member.user_id === userId);
}
