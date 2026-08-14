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

const ROLE_ORDER: RoleName[] = [
  "president",
  "vice_president",
  "treasurer",
  "secretary",
  "historian",
  "member",
  "pledge",
  "alumni",
];

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

/** E-board roles pop with the accent Chip; pledges get the pending-flavored warning Chip. */
const ROLE_CHIP_VARIANT: Record<RoleName, ChipVariant> = {
  president: "accent",
  vice_president: "accent",
  treasurer: "accent",
  secretary: "accent",
  historian: "accent",
  member: "neutral",
  pledge: "warning",
  alumni: "neutral",
};

/** Fallback label for the rare row whose display_name comes back empty. */
function shortUserId(userId: string): string {
  return userId.length > 12 ? `${userId.slice(0, 6)}…${userId.slice(-4)}` : userId;
}

export default function MembersScreen() {
  const { sessionStatus, membership, chapterLoading } = useOwnChapter();
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
  const groups = ROLE_ORDER.map((role) => ({
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
                title={ROLE_LABELS[role]}
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
                        <Chip label={ROLE_CHIP_LABELS[role]} variant={ROLE_CHIP_VARIANT[role]} />
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
