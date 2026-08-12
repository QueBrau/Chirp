/** Chapter hub: role-gated cards grid — Tree + Members for all, Treasurer/Secretary by role. */

import { useRouter, type Href } from "expo-router";
import { View } from "react-native";

import type { RoleName } from "@/api/chapters";
import { AppText, Badge, Card, Screen } from "@/components";
import { MOCK_CHAPTER, MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
import { spacing } from "@/theme";

interface Tile {
  href: Href;
  title: string;
  description: string;
  /** undefined = visible to every member. */
  roles?: RoleName[];
}

const TILES: Tile[] = [
  { href: "/chapter/tree", title: "Family Tree", description: "Bigs, littles, and lineage" },
  { href: "/chapter/members", title: "Members", description: "Roster by role" },
  {
    href: "/chapter/treasurer",
    title: "Treasurer",
    description: "Dues and the ledger",
    roles: ["treasurer", "president"],
  },
  {
    href: "/chapter/secretary",
    title: "Secretary",
    description: "Minutes and attendance",
    roles: ["secretary", "president"],
  },
];

const ROLE_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Member",
  pledge: "Pledge",
  alumni: "Alumni",
};

export default function ChapterScreen() {
  const router = useRouter();
  // Current role from mocks; real role comes from the memberships lookup (org-scope middleware).
  const role = MOCK_CURRENT_MEMBERSHIP.role;
  const visible = TILES.filter((tile) => tile.roles === undefined || tile.roles.includes(role));

  return (
    <Screen
      title="Chapter"
      subtitle={`${MOCK_CHAPTER.org_name} ${MOCK_CHAPTER.chapter_name ?? ""}`.trim()}
    >
      <View style={{ marginBottom: spacing.lg }}>
        <Badge label={`You: ${ROLE_LABELS[role]}`} tone="accent" />
      </View>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.md }}>
        {visible.map((tile) => (
          <Card
            key={tile.title}
            onPress={() => router.push(tile.href)}
            style={{ flexBasis: "47%", flexGrow: 1 }}
          >
            <View style={{ gap: spacing.sm }}>
              <AppText variant="title">{tile.title}</AppText>
              <AppText variant="caption" tone="secondary">
                {tile.description}
              </AppText>
            </View>
          </Card>
        ))}
      </View>
    </Screen>
  );
}
