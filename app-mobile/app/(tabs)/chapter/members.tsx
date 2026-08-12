/** Members: chapter roster grouped by role — SectionHeader per group, GradientAvatar rows, role Chips. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listMembers, type MembershipOut, type RoleName } from "@/api/chapters";
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
import { MOCK_CURRENT_MEMBERSHIP, mockUserById } from "@/mocks/data";
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

export default function MembersScreen() {
  const [members, setMembers] = useState<MembershipOut[] | null>(null);

  useEffect(() => {
    void listMembers(MOCK_CURRENT_MEMBERSHIP.chapter_id).then(setMembers);
  }, []);

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
                  const user = mockUserById(membership.user_id);
                  const name = user?.display_name ?? "Unknown";
                  return (
                    <ListRow
                      key={membership.id}
                      title={name}
                      subtitle={membership.pledge_class ?? undefined}
                      left={<GradientAvatar name={name} size={40} />}
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
