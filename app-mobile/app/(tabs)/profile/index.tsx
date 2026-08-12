/** Profile: own identity + chapter role, alumni fields when account_type=alumni, settings stub. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { getMyAlumniProfile, type AlumniProfileOut } from "@/api/alumni";
import { AppText, Avatar, Badge, Card, ListRow, Screen } from "@/components";
import { MOCK_CHAPTER, MOCK_CURRENT_MEMBERSHIP, MOCK_CURRENT_USER } from "@/mocks/data";
import { spacing } from "@/theme";

const ACCOUNT_TYPE_LABELS = {
  greek: "Greek member",
  non_greek: "Non-greek",
  alumni: "Alumni",
} as const;

export default function ProfileScreen() {
  const user = MOCK_CURRENT_USER;
  const [alumniProfile, setAlumniProfile] = useState<AlumniProfileOut | null>(null);

  useEffect(() => {
    if (user.account_type === "alumni") {
      void getMyAlumniProfile().then(setAlumniProfile);
    }
  }, [user.account_type]);

  return (
    <Screen title="Profile">
      <View style={{ gap: spacing.md }}>
        <Card>
          <View style={{ alignItems: "center", gap: spacing.sm }}>
            <Avatar name={user.display_name} size={72} />
            <AppText variant="title">{user.display_name}</AppText>
            <AppText variant="caption" tone="secondary">
              {user.email}
            </AppText>
            <Badge label={ACCOUNT_TYPE_LABELS[user.account_type]} tone="accent" />
          </View>
        </Card>

        <Card>
          <AppText variant="title" style={{ marginBottom: spacing.sm }}>
            Chapter
          </AppText>
          <ListRow
            title={`${MOCK_CHAPTER.org_name} ${MOCK_CHAPTER.chapter_name ?? ""}`.trim()}
            subtitle={`Role: ${MOCK_CURRENT_MEMBERSHIP.role} · ${MOCK_CURRENT_MEMBERSHIP.pledge_class ?? ""}`}
            divider={false}
          />
        </Card>

        {user.account_type === "alumni" ? (
          <Card>
            <AppText variant="title" style={{ marginBottom: spacing.sm }}>
              Alumni profile
            </AppText>
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
          </Card>
        ) : null}

        <Card>
          <AppText variant="title" style={{ marginBottom: spacing.sm }}>
            Settings
          </AppText>
          {/* Settings are a stub — notifications, appearance, and sign-out wire up with auth. */}
          <ListRow title="Notifications" subtitle="Content-free push — TODO(milestone-4)" />
          <ListRow title="Appearance" subtitle="Follows system light/dark" />
          <ListRow title="Sign out" subtitle="TODO(milestone-1): Firebase Auth" divider={false} />
        </Card>
      </View>
    </Screen>
  );
}
