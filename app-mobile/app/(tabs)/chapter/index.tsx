/**
 * Orgs tab per DESIGN §6 — two states:
 * member: HeroCard org identity + role Chip, then role-gated tool grid;
 * non-member (behind mockIsOrgMember): "Find your org" — invite code input,
 * category chips, EmptyState. Route dir stays `chapter/` for backend parity.
 */

import { useRouter, type Href } from "expo-router";
import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import { joinChapter, type RoleName } from "@/api/chapters";
import {
  AppText,
  Button,
  Card,
  Chip,
  EmptyState,
  HeroCard,
  Screen,
  SectionHeader,
} from "@/components";
import { MOCK_CAMPUS, MOCK_CHAPTER, MOCK_CURRENT_MEMBERSHIP, mockIsOrgMember } from "@/mocks/data";
import { radii, spacing, typography, useTheme } from "@/theme";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

interface Tool {
  href: Href;
  icon: FeatherIconName;
  title: string;
  description: string;
  /** undefined = visible to every member. */
  roles?: RoleName[];
}

const TOOLS: Tool[] = [
  { href: "/chapter/tree", icon: "git-branch", title: "Family Tree", description: "Bigs, littles, and lineage" },
  { href: "/chapter/members", icon: "users", title: "Members", description: "The full roster, by role" },
  {
    href: "/chapter/treasurer",
    icon: "dollar-sign",
    title: "Treasurer",
    description: "Dues and the ledger",
    roles: ["treasurer", "president"],
  },
  {
    href: "/chapter/secretary",
    icon: "file-text",
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
  alumni: "Alum",
};

const CATEGORIES = ["Fraternities", "Sororities", "Clubs", "Intramurals"] as const;
type Category = (typeof CATEGORIES)[number];

/** Member state: org identity hero + tool grid, gated by mock role. */
function MemberOrgHub() {
  const router = useRouter();
  const palette = useTheme();
  // Current role from mocks; real role comes from the memberships lookup (org-scope middleware).
  const role = MOCK_CURRENT_MEMBERSHIP.role;
  const visible = TOOLS.filter((tool) => tool.roles === undefined || tool.roles.includes(role));

  return (
    <View style={{ gap: spacing.xl }}>
      <HeroCard>
        <View style={{ gap: spacing.sm }}>
          <AppText variant="micro" tone="onAccent">
            Your org
          </AppText>
          <AppText variant="title" tone="onAccent">
            {MOCK_CHAPTER.org_name}
          </AppText>
          <AppText variant="caption" tone="onAccent">
            {MOCK_CHAPTER.chapter_name !== null
              ? `${MOCK_CHAPTER.chapter_name} · ${MOCK_CAMPUS.name}`
              : MOCK_CAMPUS.name}
          </AppText>
          <Chip label={ROLE_LABELS[role]} variant="accent" style={{ marginTop: spacing.xs }} />
        </View>
      </HeroCard>

      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.md }}>
        {visible.map((tool) => (
          <Card
            key={tool.title}
            onPress={() => router.push(tool.href)}
            style={{ flexBasis: "47%", flexGrow: 1 }}
          >
            <View style={{ gap: spacing.sm }}>
              <Feather name={tool.icon} size={typography.title.fontSize} color={palette.accent} />
              <AppText variant="headline">{tool.title}</AppText>
              <AppText variant="caption" tone="secondary">
                {tool.description}
              </AppText>
            </View>
          </Card>
        ))}
      </View>
    </View>
  );
}

/** Non-member state per DESIGN §6: invite code entry + browsable categories (greek is opt-in here). */
function FindYourOrg() {
  const palette = useTheme();
  const [code, setCode] = useState("");
  const [category, setCategory] = useState<Category>("Fraternities");

  return (
    <View style={{ gap: spacing.xl }}>
      <Card>
        <View style={{ gap: spacing.md }}>
          <AppText variant="headline">Have an invite code?</AppText>
          <TextInput
            value={code}
            onChangeText={setCode}
            placeholder="e.g. SIGCHI-EM-F26"
            placeholderTextColor={palette.inkFaint}
            autoCapitalize="characters"
            autoCorrect={false}
            style={{
              ...typography.body,
              color: palette.ink,
              backgroundColor: palette.surfaceAlt,
              borderRadius: radii.input,
              paddingHorizontal: spacing.lg,
              paddingVertical: spacing.md,
            }}
          />
          <Button
            label="Join with code"
            disabled={code.trim().length === 0}
            onPress={() => void joinChapter(code.trim())}
          />
        </View>
      </Card>

      <View>
        <SectionHeader title="Browse by category" caption="Every kind of org lives here" />
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
          {CATEGORIES.map((option) => (
            <Pressable
              key={option}
              accessibilityRole="button"
              accessibilityState={{ selected: category === option }}
              onPress={() => setCategory(option)}
              style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
            >
              <Chip label={option} variant={category === option ? "accent" : "neutral"} />
            </Pressable>
          ))}
        </View>
        <EmptyState
          title="Org discovery is coming"
          message={`Browsing ${category.toLowerCase()} lands soon — join with an invite code for now.`}
        />
      </View>
    </View>
  );
}

export default function OrgsScreen() {
  return (
    <Screen
      title="Orgs"
      subtitle={mockIsOrgMember ? MOCK_CAMPUS.name : `Find your org at ${MOCK_CAMPUS.name}`}
    >
      {mockIsOrgMember ? <MemberOrgHub /> : <FindYourOrg />}
    </Screen>
  );
}
