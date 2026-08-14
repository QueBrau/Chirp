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
import { getCampus, type AccountType, type CampusOut } from "@/api/auth";
import { myMemberships, type MyMembershipOut, type RoleName } from "@/api/chapters";
import { listPosts } from "@/api/feed";
import { hasFirebaseConfig, signOutUser, useSession } from "@/auth";
import { AppText, Card, Chip, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
// mockProfileLayout is a LOCAL UI preference (section order/visibility), not
// identity data — no backend concept of a saved layout exists yet, so this
// stays mock-seeded on purpose (see the top-of-file comment).
import { mockProfileLayout, type ProfileSectionKey, type ProfileSectionLayout } from "@/mocks/data";
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
  // The (tabs) layout only clears "loading"/"signedOut"/"unregistered" before
  // this screen can mount (see app/(tabs)/_layout.tsx) — status "ready" means a
  // real Firebase-backed identity except in Firebase-less demo mode, where
  // `user` stays null forever. The loading gate below covers both.
  const { status, user } = useSession();
  const userId = user?.id ?? null;
  const accountType = user?.account_type ?? null;
  const campusId = user?.campus_id ?? null;

  const [alumniProfile, setAlumniProfile] = useState<AlumniProfileOut | null>(null);
  // null = not yet resolved; [] is a legitimate "no chapter" result (e.g. a
  // non-greek student) — the distinction is what the loading gate below relies on.
  const [memberships, setMemberships] = useState<MyMembershipOut[] | null>(null);
  const [campus, setCampus] = useState<CampusOut | null>(null);
  const [postCount, setPostCount] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const [layout, setLayout] = useState<ProfileSectionLayout[]>(() =>
    mockProfileLayout.map((section) => ({ ...section })),
  );

  useEffect(() => {
    if (accountType !== "alumni") return;
    // Fail soft: matches the repo pattern elsewhere in this stack.
    void getMyAlumniProfile().then(setAlumniProfile).catch(() => setAlumniProfile(null));
  }, [accountType]);

  useEffect(() => {
    if (userId === null) {
      setMemberships(null);
      return;
    }
    // GET /me/memberships is the only call that joins org_name/chapter_name
    // (fetchMe()'s embedded memberships, used elsewhere via useSession(), don't).
    myMemberships()
      .then(setMemberships)
      .catch(() => setMemberships([]));
  }, [userId]);

  useEffect(() => {
    if (campusId === null) {
      setCampus(null);
      return;
    }
    getCampus(campusId)
      .then(setCampus)
      .catch(() => setCampus(null));
  }, [campusId]);

  // Single-org world for now: memberships[0] is the caller's only chapter.
  const membership = memberships?.[0] ?? null;
  const chapterId = membership?.chapter_id ?? null;

  useEffect(() => {
    if (memberships === null) return; // wait for the membership fetch to settle first
    if (chapterId === null || userId === null) {
      setPostCount(0);
      return;
    }
    listPosts(chapterId)
      .then((posts) => setPostCount(posts.filter((post) => post.author_id === userId).length))
      .catch(() => setPostCount(0));
  }, [memberships, chapterId, userId]);

  /** Finding 12: Sign out was unreachable (no onPress). Firebase isn't configured yet
   * (see src/auth/config.ts), so mock mode just returns to sign-in without a real
   * signOutUser() call — hasFirebaseConfig() gates which path runs. */
  const handleSignOut = async () => {
    if (hasFirebaseConfig()) {
      await signOutUser();
    }
    router.replace("/sign-in");
  };

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

  // A real user's identity (name/avatar/account type/campus/chapter/role/post
  // count) must never flash empty or stale while any of it is still in flight —
  // same rule as chapter/members.tsx's `loading` gate.
  const loading =
    status === "loading" || user === null || memberships === null || postCount === null;

  if (loading || user === null) {
    return (
      <Screen title="Profile">
        <EmptyState title="Loading profile..." />
      </Screen>
    );
  }

  // Guaranteed non-null by the loading gate above; `?? 0`/`?? ""` below are
  // for TypeScript's benefit only (loading's `||` chain doesn't narrow through).
  const count = postCount ?? 0;
  const chapterName =
    membership?.chapter_name != null && membership.org_name != null
      ? `${membership.org_name} ${membership.chapter_name}`
      : (membership?.org_name ?? null);
  const subtitle =
    campus !== null
      ? `${ACCOUNT_TYPE_LABELS[user.account_type]} · ${campus.name}`
      : ACCOUNT_TYPE_LABELS[user.account_type];

  return (
    <Screen title="Profile" subtitle={subtitle}>
      <View style={{ alignItems: "flex-end", marginBottom: spacing.sm }}>
        <EditLayoutToggle editing={editing} onPress={() => setEditing((value) => !value)} />
      </View>

      <View style={{ alignItems: "center", gap: spacing.sm, marginBottom: spacing.xl }}>
        <GradientAvatar name={user.display_name} size={64} photoUrl={user.avatar_url} />
        <AppText variant="title">{user.display_name}</AppText>
        {membership !== null ? (
          <View style={{ flexDirection: "row", gap: spacing.sm }}>
            <Chip label={ROLE_LABELS[membership.role]} variant="accent" />
            {membership.pledge_class !== null ? (
              <Chip label={membership.pledge_class} variant="neutral" />
            ) : null}
          </View>
        ) : null}
      </View>

      <View style={{ gap: spacing.md }}>
        {layout.map((section, index) => {
          // Structural applicability — independent of the visibility toggle
          // below: these sections don't exist for this identity/account at all
          // (no membership => no chapter/role/post-count to show; non-alumni
          // => no alumni info), so they never appear even in edit mode.
          if (section.key === "alumni" && user.account_type !== "alumni") return null;
          if ((section.key === "orgs" || section.key === "activity") && membership === null) {
            return null;
          }
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
                // Static placeholder copy: the backend has no user bio field yet,
                // so there is no real value to source this from.
                <AppText tone="secondary">
                  Sophomore · Business · here for the group chats and the intramural fields.
                </AppText>
              ) : null}

              {section.key === "orgs" && membership !== null ? (
                <ListRow
                  title={chapterName ?? ""}
                  subtitle={campus?.name}
                  right={<Chip label={ROLE_LABELS[membership.role]} variant="accent" />}
                  divider={false}
                />
              ) : null}

              {section.key === "activity" ? (
                <View style={{ flexDirection: "row", alignItems: "baseline", gap: spacing.sm }}>
                  <AppText variant="stat">{count}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {count === 1 ? "post" : "posts"} to the chapter feed
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
                    subtitle="You'll return to the sign-in screen"
                    left={<SettingsIconWell name="log-out" />}
                    onPress={() => void handleSignOut()}
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
