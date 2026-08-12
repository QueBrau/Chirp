/** Members: chapter roster grouped by role, e-board first. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listMembers, type MembershipOut, type RoleName } from "@/api/chapters";
import { AppText, Avatar, Card, EmptyState, ListRow, Screen } from "@/components";
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
        <View style={{ gap: spacing.md }}>
          {groups.map(({ role, rows }) => (
            <Card key={role}>
              <AppText
                variant="caption"
                tone="tertiary"
                style={{ marginBottom: spacing.xs, textTransform: "uppercase", letterSpacing: 1 }}
              >
                {ROLE_LABELS[role]}
              </AppText>
              {rows.map((membership, index) => {
                const user = mockUserById(membership.user_id);
                const name = user?.display_name ?? "Unknown";
                return (
                  <ListRow
                    key={membership.id}
                    title={name}
                    subtitle={membership.pledge_class ?? undefined}
                    left={<Avatar name={name} ghost={user?.is_ghost ?? false} />}
                    divider={index < rows.length - 1}
                  />
                );
              })}
            </Card>
          ))}
        </View>
      )}
    </Screen>
  );
}
