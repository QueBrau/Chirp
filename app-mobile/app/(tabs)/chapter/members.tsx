/** Members: chapter roster grouped by role — SectionHeader per group, GradientAvatar rows, role Chips. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listMembers, type MemberOut, type RoleName } from "@/api/chapters";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import {
  AppText,
  Chip,
  type ChipVariant,
  Card,
  EmptyState,
  GradientAvatar,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { spacing } from "@/theme";

const ROLE_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Members",
  pledge: "Pledges",
  alumni: "Alumni",
};

/** Singular label for the per-row role Chip (section headers use the plural ROLE_LABELS). */
const ROLE_CHIP_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Member",
  pledge: "Pledge",
  alumni: "Alum",
};

/** Prettified fallback for a role the closed label records don't know yet — the
 * server owns the taxonomy (c44), so an unmapped value degrades gracefully. */
function prettifyRole(role: string): string {
  return role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** E-board roles pop with the accent Chip (e-board set comes from role-meta, c44);
 * pledges keep the pending-flavored warning Chip — a purely cosmetic, local rule. */
function chipVariant(role: RoleName, eboard: RoleName[]): ChipVariant {
  if (eboard.includes(role)) return "accent";
  return role === "pledge" ? "warning" : "neutral";
}

/** Fallback label for the rare row whose display_name comes back empty. */
function shortUserId(userId: string): string {
  return userId.length > 12 ? `${userId.slice(0, 6)}…${userId.slice(-4)}` : userId;
}

export default function MembersScreen() {
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
                title={ROLE_LABELS[role] ?? prettifyRole(role)}
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
                          label={ROLE_CHIP_LABELS[role] ?? prettifyRole(role)}
                          variant={chipVariant(role, roleMeta?.eboard ?? [])}
                        />
                      }
                      divider={index < rows.length - 1}
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
