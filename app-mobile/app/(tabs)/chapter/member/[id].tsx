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
 * it — i.e. an actual PATCH closed a prior open term. That happens exactly
 * when this membership has a CLOSED term in its history. A term with no
 * predecessor was instead seeded either by the 0021 backfill (every
 * membership that already existed) or by open_initial_term at membership
 * creation — both stamp `started_at` at migration/creation time, not the
 * date the member actually took the role, and both leave `changed_by` null
 * for the same reason. So: the open term shows "Since <date>" ONLY when an
 * earlier, closed term exists; a sole open term with no history shows just
 * the role name, no date, rather than assert a start date this system never
 * actually recorded. Closed terms always show their full span — both of
 * THEIR boundaries are real PATCH events (the one that opened it and the one
 * that closed it), never backfilled.
 */
function termDateLabel(term: RoleTerm, hasPredecessor: boolean): string | null {
  if (term.ended_at === null) {
    return hasPredecessor ? `Since ${monthYear(term.started_at)}` : null;
  }
  return `${monthYear(term.started_at)} – ${monthYear(term.ended_at)}`;
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
  const openIndex = terms.findIndex((term) => term.ended_at === null);
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
                  const hasPredecessor = index === openIndex && terms.length > openIndex + 1;
                  const dateLabel = termDateLabel(term, hasPredecessor);
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
