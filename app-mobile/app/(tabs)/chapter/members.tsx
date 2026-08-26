/** Members: chapter roster grouped by role — SectionHeader per group, GradientAvatar rows, role Chips. */

import { useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { View } from "react-native";

import { listMembers, type MemberOut, type RoleName } from "@/api/chapters";
import { chipVariant, prettifyRole, roleLabel } from "@/lib/roleTerms";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, Chip, Card, EmptyState, GradientAvatar, ListRow, Screen, SectionHeader } from "@/components";
import { spacing } from "@/theme";

/**
 * Section-header labels, PLURAL — deliberately NOT the shared roleTerms.ts
 * ROLE_LABELS (which is singular, "Member"/"Pledge"/"Alum"): this screen's
 * group headers read "5 Pledges", not "5 Pledge". Kept local on purpose
 * (c187 audit): the per-row Chip below uses the shared singular roleLabel().
 */
const SECTION_ROLE_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Members",
  pledge: "Pledges",
  alumni: "Alumni",
};

/** Fallback label for the rare row whose display_name comes back empty. */
function shortUserId(userId: string): string {
  return userId.length > 12 ? `${userId.slice(0, 6)}…${userId.slice(-4)}` : userId;
}

export default function MembersScreen() {
  const router = useRouter();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;
  const [members, setMembers] = useState<MemberOut[] | null>(null);

  useEffect(() => {
    if (chapterId === null) {
      setMembers(null);
      return;
    }
    // Fail soft: matches the repo pattern elsewhere in this stack.
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setMembers([]));
  }, [chapterId]);

  // Session-status gating (PR #6 review): a real member's roster must never
  // flash "No members" while the session/chapter/list are still resolving —
  // same rule as chapter/index.tsx.
  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading) || members === null;

  const active = (members ?? []).filter((m) => m.status === "active");
  // Group order comes from the server taxonomy (role-meta, c44). Fail soft on a
  // null roleMeta — first-seen order still renders the full roster — and append
  // any role present in the data but missing from the taxonomy so nobody vanishes.
  const known = roleMeta?.roles ?? [];
  const seen = [...new Set(active.map((m) => m.role))];
  const roleOrder = [...known, ...seen.filter((role) => !known.includes(role))];
  const groups = roleOrder.map((role) => ({
    role,
    rows: active.filter((m) => m.role === role),
  })).filter((group) => group.rows.length > 0);

  return (
    <Screen title="Members" subtitle={loading ? undefined : `${active.length} active`}>
      {loading ? (
        <EmptyState title="Loading members..." />
      ) : active.length === 0 ? (
        <EmptyState title="No members" message="Invite your chapter to get the roster going." />
      ) : (
        <View style={{ gap: spacing.xl }}>
          {groups.map(({ role, rows }) => (
            <View key={role}>
              <SectionHeader
                title={SECTION_ROLE_LABELS[role] ?? prettifyRole(role)}
                caption={`${rows.length} ${rows.length === 1 ? "member" : "members"}`}
              />
              <Card>
                {rows.map((member, index) => {
                  const label = member.display_name.length > 0 ? member.display_name : shortUserId(member.user_id);
                  return (
                    <ListRow
                      key={member.id}
                      title={label}
                      subtitle={member.pledge_class ?? undefined}
                      left={<GradientAvatar name={label} size={40} photoUrl={member.avatar_url} />}
                      right={
                        <Chip
                          label={roleLabel(role)}
                          variant={chipVariant(role, roleMeta?.eboard ?? [])}
                        />
                      }
                      divider={index < rows.length - 1}
                      // Opens the member's own detail surface (c180), which is
                      // where role-term history (c83) lives — the roster row
                      // itself only has room for the CURRENT role Chip.
                      onPress={() => router.push(`/chapter/member/${member.user_id}`)}
                    />
                  );
                })}
              </Card>
            </View>
          ))}
        </View>
      )}
    </Screen>
  );
}
