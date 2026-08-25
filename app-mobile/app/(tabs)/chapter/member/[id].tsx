/**
 * Member detail (board card c180): the roster's per-member surface, added
 * because c83 shipped role-term history on the backend but the roster
 * (members.tsx) only has room for the member's CURRENT role Chip. Header
 * mirrors Profile's centered-identity block (DESIGN §7: GradientAvatar 64 +
 * name + role Chips); below it, a "Role history" section renders every dated
 * role span from GET /chapters/{id}/members/{userId}/role-terms, newest first.
 *
 * There is no GET /users/{id} — member identity (name/photo/pledge_class)
 * comes from the roster fetch, same resolve-against-the-roster pattern as
 * chapter/event/[id].tsx and chapter/index.tsx's own findMember helpers.
 */

import { useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";

import { getRoleTerms, listMembers, type MemberOut, type RoleName, type RoleTerm } from "@/api/chapters";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, Card, Chip, type ChipVariant, EmptyState, GradientAvatar, Screen, SectionHeader } from "@/components";
import { spacing, useTheme } from "@/theme";

const ROLE_LABELS: Record<RoleName, string> = {
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
function prettifyRole(role: string): string {
  return role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function roleLabel(role: RoleName): string {
  return ROLE_LABELS[role] ?? prettifyRole(role);
}

function chipVariant(role: RoleName, eboard: RoleName[]): ChipVariant {
  if (eboard.includes(role)) return "accent";
  return role === "pledge" ? "warning" : "neutral";
}

/** Resolve a user id against the chapter roster — the only name/photo source
 * available (mirrors the helper in chapter/event/[id].tsx). */
function findMember(members: MemberOut[], userId: string): MemberOut | undefined {
  return members.find((member) => member.user_id === userId);
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
function termDateLabel(term: RoleTerm): string | null {
  const startIsReal = term.changed_by !== null;
  if (term.ended_at === null) {
    return startIsReal ? `Since ${monthYear(term.started_at)}` : null;
  }
  return startIsReal
    ? `${monthYear(term.started_at)} – ${monthYear(term.ended_at)}`
    : `Until ${monthYear(term.ended_at)}`;
}

export default function MemberDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const palette = useTheme();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;

  const [member, setMember] = useState<MemberOut | null | undefined>(undefined);
  const [terms, setTerms] = useState<RoleTerm[]>([]);

  const load = useCallback(async () => {
    if (chapterId === null || !id) {
      setMember(null);
      return;
    }
    // Roster (for name/photo/pledge_class) and role history load together —
    // same one-round-trip-per-concern pattern as event/[id].tsx.
    const [roster, history] = await Promise.all([listMembers(chapterId), getRoleTerms(chapterId, id)]);
    const found = findMember(roster, id) ?? null;
    setMember(found);
    setTerms(history);
  }, [chapterId, id]);

  useEffect(() => {
    // Session-status gating (matches members.tsx / event/[id].tsx): don't
    // fetch — and don't fall through to "Member not found" — while the
    // session/chapter are still resolving.
    if (sessionStatus === "loading" || (membership !== null && chapterLoading)) return;
    load().catch(() => setMember(null));
  }, [load, sessionStatus, membership, chapterLoading]);

  // Session-status gating (matches members.tsx): a real member's history must
  // never flash "not found" while the session/chapter/roster are still
  // resolving.
  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading) || member === undefined;
  const label = member ? (member.display_name.length > 0 ? member.display_name : member.user_id) : "";

  return (
    <Screen title="Member">
      {loading ? (
        <EmptyState title="Loading member..." />
      ) : member === null ? (
        <EmptyState title="Member not found" message="This member may have left the chapter." />
      ) : (
        <>
          <View style={{ alignItems: "center", gap: spacing.sm, marginBottom: spacing.xl }}>
            <GradientAvatar name={label} size={64} photoUrl={member.avatar_url} />
            <AppText variant="title">{label}</AppText>
            <View style={{ flexDirection: "row", gap: spacing.sm }}>
              <Chip label={roleLabel(member.role)} variant={chipVariant(member.role, roleMeta?.eboard ?? [])} />
              {member.pledge_class !== null ? <Chip label={member.pledge_class} variant="neutral" /> : null}
            </View>
          </View>

          <View>
            <SectionHeader title="Role history" caption={`${terms.length} ${terms.length === 1 ? "term" : "terms"}`} />
            {terms.length === 0 ? (
              <Card>
                <AppText variant="body" tone="secondary">
                  No role history recorded yet.
                </AppText>
              </Card>
            ) : (
              <Card>
                {terms.map((term, index) => {
                  const dateLabel = termDateLabel(term);
                  return (
                    <View
                      key={term.id}
                      style={{
                        flexDirection: "row",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: spacing.md,
                        paddingVertical: spacing.md,
                        borderBottomWidth: index < terms.length - 1 ? StyleSheet.hairlineWidth : 0,
                        borderBottomColor: palette.border,
                      }}
                    >
                      <Chip label={roleLabel(term.role)} variant={chipVariant(term.role, roleMeta?.eboard ?? [])} />
                      {dateLabel !== null ? (
                        <AppText variant="caption" tone="secondary">
                          {dateLabel}
                        </AppText>
                      ) : null}
                    </View>
                  );
                })}
              </Card>
            )}
          </View>
        </>
      )}
    </Screen>
  );
}
