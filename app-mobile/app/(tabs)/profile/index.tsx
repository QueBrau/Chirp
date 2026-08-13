/**
 * Profile per DESIGN.md §7: centered GradientAvatar 64 + name + role Chips, then
 * USER-ARRANGEABLE section cards (About, My Orgs, Activity, Alumni info, Settings).
 * "Edit layout" ghost toggle reveals Feather chevron-up/down (reorder) and
 * eye/eye-off (visibility) per card. Order + visibility live in local state seeded
 * from the additive mockProfileLayout — mock persistence for now.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useEffect, useState, type ComponentProps } from "react";
import { Pressable, View } from "react-native";

import { getMyAlumniProfile, type AlumniProfileOut } from "@/api/alumni";
import type { AccountType } from "@/api/auth";
import type { RoleName } from "@/api/chapters";
import { AppText, Card, Chip, GradientAvatar, ListRow, Screen } from "@/components";
import {
  MOCK_CAMPUS,
  MOCK_CHAPTER,
  MOCK_CURRENT_MEMBERSHIP,
  MOCK_CURRENT_USER,
  MOCK_POSTS,
  mockProfileLayout,
  type ProfileSectionKey,
  type ProfileSectionLayout,
} from "@/mocks/data";
import { radii, spacing, typography, useTheme } from "@/theme";

type FeatherName = ComponentProps<typeof Feather>["name"];

const ACCOUNT_TYPE_LABELS: Record<AccountType, string> = {
  non_greek: "Student",
  greek: "Fraternity or sorority member",
  alumni: "Alum",
};

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

const SECTION_TITLES: Record<ProfileSectionKey, string> = {
  about: "About",
  orgs: "My Orgs",
  activity: "Activity",
  alumni: "Alumni info",
  settings: "Settings",
};

/** Small ghost pill by the header — pencil while browsing, check while arranging. */
function EditLayoutToggle({ editing, onPress }: { editing: boolean; onPress: () => void }) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={editing ? "Done editing layout" : "Edit layout"}
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.xs,
        paddingHorizontal: spacing.lg,
        paddingVertical: spacing.sm,
        borderRadius: radii.pill,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather
        name={editing ? "check" : "edit-2"}
        size={typography.caption.fontSize}
        color={palette.inkSecondary}
      />
      <AppText variant="bodyBold" tone="secondary">
        {editing ? "Done" : "Edit layout"}
      </AppText>
    </Pressable>
  );
}

/** Round Feather glyph button used for the reorder/visibility controls in edit mode. */
function EditControl({
  name,
  label,
  disabled = false,
  onPress,
}: {
  name: FeatherName;
  label: string;
  disabled?: boolean;
  onPress: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        width: spacing.xxl,
        height: spacing.xxl,
        borderRadius: radii.pill,
        backgroundColor: palette.surfaceAlt,
        alignItems: "center",
        justifyContent: "center",
        opacity: disabled ? 0.35 : pressed ? 0.7 : 1,
      })}
    >
      <Feather name={name} size={typography.body.fontSize} color={palette.inkSecondary} />
    </Pressable>
  );
}

/** Tinted round well for a Settings row's leading Feather glyph. */
function SettingsIconWell({ name }: { name: FeatherName }) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: spacing.xxl,
        height: spacing.xxl,
        borderRadius: radii.pill,
        backgroundColor: palette.surfaceAlt,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Feather name={name} size={typography.headline.fontSize} color={palette.inkSecondary} />
    </View>
  );
}

export default function ProfileScreen() {
  const router = useRouter();
  const user = MOCK_CURRENT_USER;
  const [alumniProfile, setAlumniProfile] = useState<AlumniProfileOut | null>(null);
  const [editing, setEditing] = useState(false);
  const [layout, setLayout] = useState<ProfileSectionLayout[]>(() =>
    mockProfileLayout
      .filter((section) => section.key !== "alumni" || user.account_type === "alumni")
      .map((section) => ({ ...section })),
  );

  useEffect(() => {
    if (user.account_type === "alumni") {
      void getMyAlumniProfile().then(setAlumniProfile);
    }
  }, [user.account_type]);

  const moveSection = (index: number, direction: -1 | 1) => {
    setLayout((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const toggleVisible = (key: ProfileSectionKey) => {
    setLayout((current) =>
      current.map((section) =>
        section.key === key ? { ...section, visible: !section.visible } : section,
      ),
    );
  };

  const postCount = MOCK_POSTS.filter((post) => post.author_id === user.id).length;
  const chapterName = MOCK_CHAPTER.chapter_name !== null
    ? `${MOCK_CHAPTER.org_name} ${MOCK_CHAPTER.chapter_name}`
    : MOCK_CHAPTER.org_name;

  return (
    <Screen title="Profile" subtitle={`${ACCOUNT_TYPE_LABELS[user.account_type]} · ${MOCK_CAMPUS.name}`}>
      <View style={{ alignItems: "flex-end", marginBottom: spacing.sm }}>
        <EditLayoutToggle editing={editing} onPress={() => setEditing((value) => !value)} />
      </View>

      <View style={{ alignItems: "center", gap: spacing.sm, marginBottom: spacing.xl }}>
        <GradientAvatar name={user.display_name} size={64} photoUrl={user.avatar_url} />
        <AppText variant="title">{user.display_name}</AppText>
        <View style={{ flexDirection: "row", gap: spacing.sm }}>
          <Chip label={ROLE_LABELS[MOCK_CURRENT_MEMBERSHIP.role]} variant="accent" />
          {MOCK_CURRENT_MEMBERSHIP.pledge_class !== null ? (
            <Chip label={MOCK_CURRENT_MEMBERSHIP.pledge_class} variant="neutral" />
          ) : null}
        </View>
      </View>

      <View style={{ gap: spacing.md }}>
        {layout.map((section, index) => {
          if (!editing && !section.visible) return null;
          const title = SECTION_TITLES[section.key];

          return (
            <Card key={section.key} style={!section.visible ? { opacity: 0.5 } : undefined}>
              <View
                style={{
                  flexDirection: "row",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: spacing.sm,
                  marginBottom: spacing.md,
                }}
              >
                <AppText variant="title">{title}</AppText>
                {editing ? (
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs }}>
                    <EditControl
                      name="chevron-up"
                      label={`Move ${title} up`}
                      disabled={index === 0}
                      onPress={() => moveSection(index, -1)}
                    />
                    <EditControl
                      name="chevron-down"
                      label={`Move ${title} down`}
                      disabled={index === layout.length - 1}
                      onPress={() => moveSection(index, 1)}
                    />
                    <EditControl
                      name={section.visible ? "eye" : "eye-off"}
                      label={section.visible ? `Hide ${title}` : `Show ${title}`}
                      onPress={() => toggleVisible(section.key)}
                    />
                  </View>
                ) : null}
              </View>

              {section.key === "about" ? (
                <AppText tone="secondary">
                  Sophomore · Business · here for the group chats and the intramural fields.
                </AppText>
              ) : null}

              {section.key === "orgs" ? (
                <ListRow
                  title={chapterName}
                  subtitle={MOCK_CAMPUS.name}
                  right={<Chip label={ROLE_LABELS[MOCK_CURRENT_MEMBERSHIP.role]} variant="accent" />}
                  divider={false}
                />
              ) : null}

              {section.key === "activity" ? (
                <View style={{ flexDirection: "row", alignItems: "baseline", gap: spacing.sm }}>
                  <AppText variant="stat">{postCount}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {postCount === 1 ? "post" : "posts"} to the chapter feed
                  </AppText>
                </View>
              ) : null}

              {section.key === "alumni" ? (
                <View>
                  <ListRow
                    title={alumniProfile?.company ?? "Add your company"}
                    subtitle={alumniProfile?.title ?? undefined}
                  />
                  <ListRow
                    title={`Class of ${alumniProfile?.grad_year ?? "—"}`}
                    subtitle={alumniProfile?.industry ?? undefined}
                  />
                  <ListRow
                    title="Mentoring"
                    subtitle={alumniProfile?.open_to_mentoring ? "Open to mentoring" : "Not right now"}
                    divider={false}
                  />
                </View>
              ) : null}

              {section.key === "settings" ? (
                <View>
                  <ListRow
                    title="Notifications"
                    subtitle="Content-free push — TODO(milestone-4)"
                    left={<SettingsIconWell name="bell" />}
                  />
                  <ListRow
                    title="Appearance"
                    subtitle="Accent color & background"
                    left={<SettingsIconWell name="moon" />}
                    onPress={() => router.push("/profile/appearance")}
                  />
                  <ListRow
                    title="Sign out"
                    subtitle="TODO(milestone-1): Firebase Auth"
                    left={<SettingsIconWell name="log-out" />}
                    divider={false}
                  />
                </View>
              ) : null}
            </Card>
          );
        })}
      </View>
    </Screen>
  );
}
