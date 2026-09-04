/**
 * Profile per DESIGN.md §7: centered GradientAvatar 64 + name + role Chips, then
 * USER-ARRANGEABLE section cards (My Orgs, Activity, Alumni info, Settings).
 * "About" is defined in SECTION_TITLES but filtered out of the layout — see c97's
 * sibling c99 at the useState below; it comes back with users.bio.
 * "Edit layout" ghost toggle reveals Feather chevron-up/down (reorder) and
 * eye/eye-off (visibility) per card. Order + visibility live in local state seeded
 * from the additive mockProfileLayout — mock persistence for now.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useCallback, useEffect, useState, type ComponentProps } from "react";
import { Pressable, View } from "react-native";

import * as ImagePicker from "expo-image-picker";

import { getMyAlumniProfile, type AlumniProfileOut } from "@/api/alumni";
import { getCampus, updateProfile, type AccountType, type CampusOut } from "@/api/auth";
import {
  getMediaUploadUrl,
  uploadMediaBytes,
  MAX_UPLOAD_BYTES,
  type AllowedMediaContentType,
} from "@/api/media";
import { myMemberships, type MyMembershipOut } from "@/api/chapters";
import { countMyPosts } from "@/api/feed";
import { hasFirebaseConfig, signOutUser, useSession } from "@/auth";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { AppText, Card, Chip, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { ROLE_LABELS } from "@/lib/roleTerms";
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
  const { status, user, refresh } = useSession();
  const [savingAvatar, setSavingAvatar] = useState(false);
  const userId = user?.id ?? null;
  const accountType = user?.account_type ?? null;
  const campusId = user?.campus_id ?? null;

  const [alumniProfile, setAlumniProfile] = useState<AlumniProfileOut | null>(null);
  const [alumniLoadFailed, setAlumniLoadFailed] = useState(false);
  // null = not yet resolved; [] is a legitimate "no chapter" result (e.g. a
  // non-greek student) — the distinction is what the loading gate below relies on.
  const [memberships, setMemberships] = useState<MyMembershipOut[] | null>(null);
  const [campus, setCampus] = useState<CampusOut | null>(null);
  const [postCount, setPostCount] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const [layout, setLayout] = useState<ProfileSectionLayout[]>(() =>
    mockProfileLayout
      // c99: "About" rendered a hardcoded bio - "Sophomore, Business, here for
      // the group chats and the intramural fields" - on EVERY profile, as if it
      // were that user's own words. There is no bio field on the backend, so
      // there was nothing real to show and no way to edit it. It also flatly
      // contradicted the account type people pick at onboarding: an alum's own
      // profile called them a sophomore. Dropped rather than replaced with an
      // empty state plus an edit button, because an edit affordance over a
      // column that does not exist is the same lie in a different shape.
      // Restore this section in the commit that adds users.bio.
      .filter((section) => section.key !== "about")
      .map((section) => ({ ...section })),
  );

  /**
   * c319. The catch here used to be justified as "matches the repo pattern elsewhere in
   * this stack" — the same sentence c317 removed from two other files, and the third
   * instance of it citing the defect's own spread as its warrant.
   *
   * What it actually did: a failed fetch set the profile to null, and null renders "Add
   * your company" / "Add your class year". So an alum who filled those in months ago was
   * told, on a dropped request, that they had never filled them in — and invited to type
   * it all again. Absence presented as fact, the c299/c313 class.
   *
   * The catch lives INSIDE the callback rather than at the call site for c317's reason:
   * the retry below calls this directly, and a catch attached only to the effect would
   * leave a failed RETRY unhandled, exactly when the user is already failing.
   */
  const loadAlumniProfile = useCallback(async () => {
    setAlumniLoadFailed(false);
    try {
      setAlumniProfile(await getMyAlumniProfile());
    } catch {
      setAlumniProfile(null);
      setAlumniLoadFailed(true);
    }
  }, []);

  useEffect(() => {
    if (accountType !== "alumni") return;
    void loadAlumniProfile();
  }, [accountType, loadAlumniProfile]);

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
    // c217: this used to be listPosts(chapterId) filtered by author_id in JS. c210
    // capped that route at 50 with a cursor, so from post 51 on this screen only
    // ever saw page one and the stat quietly read low with nothing to signal it.
    // One server-side aggregate instead, applying the same visibility rules the
    // feed listing does, so the number and the feed can never disagree.
    countMyPosts(chapterId)
      .then(({ count }) => setPostCount(count))
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


  /**
   * Pick, upload, and set the caller's profile picture (board c221).
   *
   * The bytes go STRAIGHT TO GCS on a signed url and never through the Chirp API, the
   * same path post media takes. What reaches our backend is the tmp/ object_name, not a
   * url - PATCH /auth/me moves that object to avatars/ and assigns the canonical url
   * itself, so a client cannot point an avatar at an arbitrary address.
   *
   * The try/catch around the picker calls is not defensive padding: expo-image-picker
   * is a native module, so on an EAS dev build cut before it was added these throw at
   * module resolution rather than opening a picker. Without this the user taps their
   * avatar and nothing happens at all, with no message. Same precedent as
   * CreateSheet.pickPhoto and treasurer.exportCsv (c139).
   */
  const changeAvatar = async () => {
    let permission: ImagePicker.PermissionResponse;
    let result: ImagePicker.ImagePickerResult;
    try {
      permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
      if (!permission.granted) {
        showAlert(
          "Photo access needed",
          "Chirp needs access to your photos to set a profile picture. You can allow it in Settings.",
        );
        return;
      }
      result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ["images"],
        quality: 0.8,
        selectionLimit: 1,
        allowsEditing: true,
        aspect: [1, 1],
      });
    } catch {
      showAlert(
        "Can't change your picture yet",
        "Changing your picture isn't available in this version of the app yet. Ask whoever set up Chirp for your chapter to check for an update.",
      );
      return;
    }
    if (result.canceled || result.assets.length === 0) return;
    const asset = result.assets[0];

    const contentType = asset.mimeType;
    if (contentType !== "image/jpeg" && contentType !== "image/png" && contentType !== "image/webp") {
      showAlert("Unsupported photo", "Please choose a JPEG, PNG, or WebP image.");
      return;
    }
    if (asset.fileSize !== undefined && asset.fileSize > MAX_UPLOAD_BYTES) {
      showAlert("Photo too large", "Profile pictures are limited to 10MB. Try a different one.");
      return;
    }

    setSavingAvatar(true);
    try {
      const typedContentType = contentType as AllowedMediaContentType;
      const bytes = await (await fetch(asset.uri)).blob();
      const { upload_url, object_name } = await getMediaUploadUrl(typedContentType, bytes.size);
      await uploadMediaBytes(upload_url, bytes, typedContentType);
      await updateProfile({ avatar_object_name: object_name });
      // refresh() rather than a local setState: the avatar renders from useSession()'s
      // user in the tab bar and on every post this person wrote, so updating one copy
      // here would leave the rest stale until the next cold start.
      await refresh();
    } catch (error) {
      showApiError(error, "Couldn't update your picture");
    } finally {
      setSavingAvatar(false);
    }
  };

  const removeAvatar = () => {
    confirmAction({
      title: "Remove picture?",
      message: "Your profile will go back to showing your initials.",
      confirmLabel: "Remove",
      destructive: true,
      onConfirm: async () => {
        setSavingAvatar(true);
        try {
          // Explicit null, which the API reads as "clear it" - distinct from omitting
          // the field, which means "leave it alone".
          await updateProfile({ avatar_object_name: null });
          await refresh();
        } catch (error) {
          showApiError(error, "Couldn't remove your picture");
        } finally {
          setSavingAvatar(false);
        }
      },
    });
  };

  return (
    <Screen title="Profile" subtitle={subtitle}>
      <View style={{ alignItems: "flex-end", marginBottom: spacing.sm }}>
        <EditLayoutToggle editing={editing} onPress={() => setEditing((value) => !value)} />
      </View>

      <View style={{ alignItems: "center", gap: spacing.sm, marginBottom: spacing.xl }}>
        {/* The avatar is the control, so the affordance has to be on it rather than in
            a menu somewhere: tap to change, and a quiet Remove appears only once there
            is something to remove. No camera-badge overlay - DESIGN.md rule 4 spends
            the accent elsewhere on this screen, and a badge here would be a second
            loud moment competing with the role Chip below. */}
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={
            user.avatar_url !== null ? "Change your profile picture" : "Add a profile picture"
          }
          accessibilityState={{ busy: savingAvatar }}
          disabled={savingAvatar}
          onPress={() => void changeAvatar()}
          style={({ pressed }) => ({ opacity: savingAvatar ? 0.4 : pressed ? 0.6 : 1 })}
        >
          <GradientAvatar name={user.display_name} size={64} photoUrl={user.avatar_url} />
        </Pressable>
        <Pressable
          accessibilityRole="button"
          disabled={savingAvatar}
          onPress={() => (user.avatar_url !== null ? removeAvatar() : void changeAvatar())}
          style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1 })}
        >
          <AppText variant="caption" tone="secondary">
            {savingAvatar
              ? "Saving..."
              : user.avatar_url !== null
                ? "Change or remove photo"
                : "Add a photo"}
          </AppText>
        </Pressable>
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
                alumniLoadFailed ? (
                  <EmptyState
                    title="Couldn't load your alumni profile"
                    message="Check your connection and try again. This isn't a statement that you haven't filled it in."
                    actionLabel="Try again"
                    onAction={() => void loadAlumniProfile()}
                  />
                ) : (
                <View>
                  <ListRow
                    title={alumniProfile?.company ?? "Add your company"}
                    subtitle={alumniProfile?.title ?? undefined}
                  />
                  <ListRow
                    title={
                      alumniProfile?.grad_year
                        ? `Class of ${alumniProfile.grad_year}`
                        : "Add your class year"
                    }
                    subtitle={alumniProfile?.industry ?? undefined}
                  />
                  <ListRow
                    title="Mentoring"
                    subtitle={alumniProfile?.open_to_mentoring ? "Open to mentoring" : "Not right now"}
                    divider={false}
                  />
                </View>
                )
              ) : null}

              {section.key === "settings" ? (
                <View>
                  <ListRow
                    title="Notifications"
                    subtitle="Push notifications are coming soon."
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
