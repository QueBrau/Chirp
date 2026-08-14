/** Members: chapter roster grouped by role — SectionHeader per group, GradientAvatar rows, role Chips. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listMembers, type MembershipOut, type RoleName } from "@/api/chapters";
import { useSession } from "@/auth";
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

/**
 * TODO(backend): GET /chapters/{id}/members returns MembershipOut (user_id,
 * role, status) with no joined display name — there's no /users/{id} or bulk
 * name-resolution endpoint to call either (alumni.py joins display_name but
 * only within its own directory query). Until one of those exists, rows show
 * role + a shortened user id instead of a fabricated name.
 */
function shortUserId(userId: string): string {
  return userId.length > 12 ? `${userId.slice(0, 6)}…${userId.slice(-4)}` : userId;
}

export default function MembersScreen() {
  const { memberships } = useSession();
  // Single-org world for now: the roster belongs to the member's first (and
  // currently only) chapter membership.
  const chapterId = memberships[0]?.chapter_id ?? null;
  const [members, setMembers] = useState<MembershipOut[] | null>(null);

  useEffect(() => {
    if (chapterId === null) {
      setMembers([]);
      return;
    }
    // Fail soft: matches the repo pattern elsewhere in this stack.
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setMembers([]));
  }, [chapterId]);

  const active = (members ?? []).filter((m) => m.status === "active");
  const groups = ROLE_ORDER.map((role) => ({
    role,
    rows: active.filter((m) => m.role === role),
  })).filter((group) => group.rows.length > 0);

  return (
    <Screen title="Members" subtitle={`${active.length} active`}>
      {members !== null && active.length === 0 ? (
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
                {rows.map((membership, index) => {
                  const label = shortUserId(membership.user_id);
                  return (
                    <ListRow
                      key={membership.id}
                      title={label}
                      subtitle={membership.pledge_class ?? undefined}
                      left={<GradientAvatar name={label} size={40} />}
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
