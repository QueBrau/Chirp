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
 *
 * Role display + the date-honesty rule (termDateLabel) live in
 * @/lib/roleTerms — extracted in c181 so the alumni directory and the family
 * tree reuse this screen's exact rule instead of re-deriving it.
 */

import { useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";

import { getRoleTerms, listMembers, type MemberOut, type RoleTerm } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { chipVariant, roleLabel, termDateLabel } from "@/lib/roleTerms";
import { findMember } from "@/lib/roster";
import { AppText, Card, Chip, EmptyState, GradientAvatar, Screen, SectionHeader } from "@/components";
import { spacing, useTheme } from "@/theme";

export default function MemberDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const palette = useTheme();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;

  const [member, setMember] = useState<MemberOut | null | undefined>(undefined);
  const [terms, setTerms] = useState<RoleTerm[]>([]);
  /** The fetch itself failed. Distinct from "this person is not in the chapter" (c316). */
  const [loadFailed, setLoadFailed] = useState(false);

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

  /**
   * c316: this used to be `load().catch(() => setMember(null))`, and null renders
   * "Member not found — This member may have left the chapter." So a dropped
   * connection did not merely fail to answer, it told the reader that a specific,
   * named person had left their chapter. Every other site in the c299 sweep said
   * "there is nothing here"; this one made a false statement about someone.
   *
   * WHICH FAILURE IS ACTUALLY AN ANSWER, and this differs from c312's version of
   * the same fix. On event/[id].tsx the server 404s for a missing event, so 404 and
   * 403 both mean "gone or not yours". Here the ordinary not-in-this-chapter case
   * does not throw at all — the roster fetch SUCCEEDS and findMember simply does not
   * find them, which is the success path above. The one exception that carries the
   * same meaning is a 404 from getRoleTerms: list_role_terms raises
   * membership_not_found when the target has no membership row for this chapter
   * (backend/app/routers/chapters.py:629), which is precisely "not a member here".
   *
   * A 403 is deliberately NOT in that set, unlike c312. It would mean the VIEWER's
   * own membership stopped being active mid-read — a statement about the reader, not
   * about the person on screen, so answering it with "they may have left" would be
   * the same lie in a new costume.
   */
  const runLoad = useCallback(() => {
    setLoadFailed(false);
    load().catch((error: unknown) => {
      const notAMember = error instanceof ApiError && error.status === 404;
      setMember(null);
      setLoadFailed(!notAMember);
    });
  }, [load]);

  useEffect(() => {
    // Session-status gating (matches members.tsx / event/[id].tsx): don't
    // fetch — and don't fall through to "Member not found" — while the
    // session/chapter are still resolving.
    if (sessionStatus === "loading" || (membership !== null && chapterLoading)) return;
    runLoad();
  }, [runLoad, sessionStatus, membership, chapterLoading]);

  // Session-status gating (matches members.tsx): a real member's history must
  // never flash "not found" while the session/chapter/roster are still
  // resolving.
  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading) || member === undefined;
  const label = member ? (member.display_name.length > 0 ? member.display_name : member.user_id) : "";

  return (
    <Screen title="Member" subtitle="Profile and role history">
      {loading ? (
        <EmptyState title="Loading member..." />
      ) : loadFailed ? (
        // Rendered BEFORE the not-found branch on purpose: `member` is null in both
        // states, so whichever comes first wins, and the wrong order here is exactly
        // the bug (c299 hit the same ordering trap on treasurer.tsx's role gate).
        <EmptyState
          title="Couldn't load this member"
          message="Something went wrong reaching the server. This isn't a statement that they left the chapter."
          actionLabel="Try again"
          onAction={runLoad}
        />
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
