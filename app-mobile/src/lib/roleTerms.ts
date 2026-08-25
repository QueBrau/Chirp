/**
 * Role display + role-term date honesty (board card c83's client rule, first
 * coded for the member detail screen in c180, extracted here in c181 so the
 * alumni directory and the family tree reuse the exact same honesty check
 * instead of re-deriving it — one definition, not three near-copies).
 */

import type { RoleName, RoleTerm } from "@/api/chapters";
import type { ChipVariant } from "@/components";

export const ROLE_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Member",
  pledge: "Pledge",
  alumni: "Alum",
};

/** Prettified fallback for a role the closed label record doesn't know yet —
 * the server owns the taxonomy (c44), mirrors members.tsx's own fallback. */
export function prettifyRole(role: string): string {
  return role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function roleLabel(role: RoleName): string {
  return ROLE_LABELS[role] ?? prettifyRole(role);
}

/**
 * Chip color for a role. `eboard` must come from the server (GET
 * .../role-meta), never a hardcoded role list — permissions.py's own comment
 * documents the bug that came from a client guessing this set (chapter/index.tsx
 * once hardcoded ["treasurer","president"] and silently dropped vice_president
 * and historian from their tools).
 */
export function chipVariant(role: RoleName, eboard: RoleName[]): ChipVariant {
  if (eboard.includes(role)) return "accent";
  return role === "pledge" ? "warning" : "neutral";
}

function monthYear(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

/**
 * HONESTY RULE (c83 migration docstring, carried into the client for c180):
 * a role term's `started_at` is only a REAL date when apply_role_change wrote
 * the term — and the data says exactly when that happened: `changed_by` is
 * non-null on precisely the rows a real PATCH created, and null on the rows
 * nobody's action dated (the 0021 backfill and open_initial_term's seed at
 * membership creation, both stamped at migration/creation time). So the start
 * date renders only when `changed_by` is set. This must key off changed_by,
 * NOT off "does a closed term exist": a SEEDED term that later gets closed
 * has a real end but still a backfilled start, and rendering its full span
 * would assert a start date this system never recorded — it shows
 * "Until <date>" instead. Every ended_at is always real (only a PATCH closes
 * a term), so end dates render unconditionally.
 */
export function termDateLabel(term: RoleTerm): string | null {
  const startIsReal = term.changed_by !== null;
  if (term.ended_at === null) {
    return startIsReal ? `Since ${monthYear(term.started_at)}` : null;
  }
  return startIsReal
    ? `${monthYear(term.started_at)} – ${monthYear(term.ended_at)}`
    : `Until ${monthYear(term.ended_at)}`;
}

/** The term with `ended_at === null`, if any — the role a member holds right
 * now. At most one open term exists per membership (0021's backfill and
 * apply_role_change both maintain that invariant), so `.find` never has to
 * pick among several. */
export function currentTerm(terms: RoleTerm[]): RoleTerm | null {
  return terms.find((term) => term.ended_at === null) ?? null;
}

/**
 * The highest-ranked e-board term this member has REALLY held — c181's
 * "President, 2026" claim on the alumni directory. `changed_by === null`
 * terms (seeded/backfilled) never win: an invented office is worse than no
 * office. Rank comes from `eboard` (server-ordered president-first, since
 * GET .../role-meta walks permissions.EBOARD in Role-enum declaration order),
 * so a role outside that list — member/pledge/alumni, or a future role this
 * caller's role-meta hasn't been told about — never wins either. Ties (two
 * terms at the same rank) keep the first one seen; callers pass `terms`
 * newest-first (the API's own order), so that's the most recent.
 */
export function highestOfficeTerm(terms: RoleTerm[], eboard: RoleName[]): RoleTerm | null {
  let best: RoleTerm | null = null;
  let bestRank = Infinity;
  for (const term of terms) {
    if (term.changed_by === null) continue;
    const rank = eboard.indexOf(term.role);
    if (rank === -1) continue;
    if (rank < bestRank) {
      best = term;
      bestRank = rank;
    }
  }
  return best;
}

/** "President, 2026" — null when no real e-board term exists. Never invents
 * a claim: no dated term proving an office means no office is shown. */
export function highestOfficeLabel(terms: RoleTerm[], eboard: RoleName[]): string | null {
  const term = highestOfficeTerm(terms, eboard);
  if (term === null) return null;
  return `${roleLabel(term.role)}, ${new Date(term.started_at).getFullYear()}`;
}
