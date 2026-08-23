/**
 * CreateSheet (DESIGN §7/§8.7): bottom sheet opened by the Home/Org FAB —
 * Photo / Video / Text rows. Text opens a real composer — body + an audience
 * picker (org/campus, board Decisions log Aug 14).
 *
 * PHOTO IS REAL NOW (c70): tapping it picks from the library (no camera capture
 * in alpha scope — a photo-library permission is a smaller ask than camera, and
 * "attach a photo" never required taking one fresh), uploads it via a signed GCS
 * url (src/api/media.ts — the client never proxies bytes through the API), and
 * lands in the same compose step Text does, now carrying a photo. VIDEO STAYS
 * DISABLED: c70's approved scope is photos on posts only.
 *
 * Caption is still required even with a photo attached — this does not relax
 * canSubmit's existing rule, on purpose: "can I post a photo with no caption" is
 * a real, separate product question nobody has answered yet, not something to
 * decide silently while wiring the upload pipeline.
 *
 * The picker itself needs expo-image-picker, a NATIVE module — it cannot run in
 * Expo Go or the current EAS dev build, only a rebuilt one (same blocker as
 * c39). Everything up to and including the picker call is real, tested code;
 * the actual "tap it and see a picker" moment is untestable until that build
 * exists. See app-mobile/src/api/media.ts and backend/app/services/
 * storage_service.py for the upload half, which IS tested (server-side, and via
 * fetch mocking here) without needing a device at all.
 *
 * Two create routes sit behind that picker (c71). A member posting to their org,
 * or to campus from inside their org, goes to POST /chapters/{chapter_id}/posts.
 * A student who belongs to no chapter goes to POST /campuses/{campus_id}/posts,
 * which only makes campus posts — so for that student the picker is not a choice
 * at all and renders as a single statement of where the post is going.
 */

import { Feather } from "@expo/vector-icons";
import * as ImagePicker from "expo-image-picker";
import type { ComponentProps } from "react";
import { useRef, useState } from "react";
import { ActivityIndicator, Alert, Image, Modal, Pressable, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { ApiError } from "@/api/client";
import { createCampusPost, createPost, type PostAudience } from "@/api/feed";
import {
  getMediaUploadUrl,
  uploadMediaBytes,
  MAX_UPLOAD_BYTES,
  type AllowedMediaContentType,
} from "@/api/media";
import { light, radii, spacing, typography, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { ListRow } from "./ListRow";

type FeatherIconName = ComponentProps<typeof Feather>["name"];
type Step = "options" | "compose";

export interface CreateSheetProps {
  visible: boolean;
  onClose: () => void;
  /**
   * Chapter to post into, or null when the caller belongs to no chapter (c71).
   * null is a supported state, not a broken one: the composer switches to the
   * campus route and drops the audience choice, because 'org' is meaningless
   * without an org.
   */
  chapterId: string | null;
  /**
   * The caller's campus. Required for a chapter-less author, since it is the
   * only thing identifying where their post goes; a member can still post
   * without it by using their chapter's route.
   */
  campusId: string | null;
  /**
   * Real campus name for the audience picker's "everyone" copy, when the
   * caller already has it on hand (Home resolves one via getCampus()).
   * Falls back to a generic phrase — chapter/index.tsx has no real
   * campus-name source for its own screen data (only the cosmetic
   * MOCK_CAMPUS label used for its header eyebrow).
   */
  campusName?: string | null;
  /**
   * Called after the post is confirmed created server-side, so a screen that
   * owns a feed list (Home, the org Feed segment) can refresh it. No global
   * refetch mechanism exists in this app, so this is opt-in per caller.
   */
  onPosted?: () => void;
}

/** ApiError carries a server-provided `.detail`; anything else gets a generic
 * fallback. Same shape as the local helper in yak/index.tsx and treasurer.tsx —
 * there's no shared one, so this mirrors it rather than inventing a new import. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

function OptionIcon({ icon, muted }: { icon: FeatherIconName; muted?: boolean }) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: 40,
        height: 40,
        borderRadius: radii.avatar,
        backgroundColor: muted ? palette.surfaceAlt : palette.accentSoft,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Feather name={icon} size={18} color={muted ? palette.inkFaint : palette.accent} />
    </View>
  );
}

/**
 * One audience choice. This is a privacy control, not a cosmetic toggle, so
 * the selected state carries three redundant signals (fill, border, and a
 * filled-vs-outline icon) rather than one subtle highlight.
 */
function AudienceChoice({
  title,
  description,
  selected,
  onPress,
}: {
  title: string;
  description: string;
  selected: boolean;
  onPress: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.md,
        padding: spacing.md,
        borderRadius: radii.input,
        backgroundColor: selected ? palette.accentSoft : palette.surfaceAlt,
        borderWidth: selected ? 2 : 1,
        borderColor: selected ? palette.accent : palette.border,
        opacity: pressed ? 0.9 : 1,
      })}
    >
      <View style={{ flex: 1, gap: spacing.xs }}>
        <AppText variant="bodyBold" tone={selected ? "accent" : "primary"}>
          {title}
        </AppText>
        <AppText variant="caption" tone="secondary">
          {description}
        </AppText>
      </View>
      <Feather
        name={selected ? "check-circle" : "circle"}
        size={20}
        color={selected ? palette.accent : palette.inkFaint}
      />
    </Pressable>
  );
}

export function CreateSheet({
  visible,
  onClose,
  chapterId,
  campusId,
  campusName,
  onPosted,
}: CreateSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [step, setStep] = useState<Step>("options");
  const [body, setBody] = useState("");
  // Never default to campus (board Decisions log, Aug 14 — mirrors the
  // server-side PostCreate.audience default): a user who never touches this
  // control must not accidentally broadcast a chapter post campus-wide.
  const [audience, setAudience] = useState<PostAudience>("org");
  // A chapter-less author has exactly one destination, so there is no choice to
  // offer and no way for the org default above to apply to them (c71).
  const canChooseAudience = chapterId !== null;
  const effectiveAudience: PostAudience = canChooseAudience ? audience : "campus";
  // preview_url of an uploaded photo, once the pick+upload round trip finishes — null
  // means "no photo attached," not "still uploading" (see uploadingPhoto). Local
  // rendering ONLY (c132): the tmp/ object this points at is never sent to the
  // backend, and is not what ends up in the post's media_urls.
  const [photoUrl, setPhotoUrl] = useState<string | null>(null);
  // The tmp/ object_name actually sent at submit time (c132) — the server moves this
  // to a permanent location and assigns the post's real media_urls itself.
  const [photoObjectName, setPhotoObjectName] = useState<string | null>(null);
  const [uploadingPhoto, setUploadingPhoto] = useState(false);
  const [posting, setPosting] = useState(false);
  // Belt-and-suspenders alongside `posting` state: a ref is read/written
  // synchronously, so two taps that both land before the first setState
  // re-render still can't both pass the guard — a hard double-submit guard,
  // not just a disabled-prop that lags one render behind.
  const postingRef = useRef(false);

  const reset = () => {
    setStep("options");
    setBody("");
    setAudience("org");
    setPhotoUrl(null);
    setPhotoObjectName(null);
    setUploadingPhoto(false);
  };

  const close = () => {
    reset();
    onClose();
  };

  // Somewhere to post is either a chapter or a campus; without both there is no
  // route at all, and Fab already declines to render in that case.
  const canSubmit =
    body.trim().length > 0 && (chapterId !== null || campusId !== null) && !posting;

  /**
   * Pick a photo from the library, then request+upload in the same tap — the user
   * sees one loading state ("Uploading...") rather than a pick step followed by a
   * separate, confusing upload step. Client-side content-type/size checks happen
   * BEFORE any network call, mirroring backend's own rules (storage_service.
   * ALLOWED_CONTENT_TYPES / MAX_UPLOAD_BYTES) so a bad pick fails instantly instead
   * of round-tripping to the server to be told the same thing.
   */
  const pickPhoto = async () => {
    let permission: ImagePicker.PermissionResponse;
    let result: ImagePicker.ImagePickerResult;
    try {
      permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
      if (!permission.granted) {
        Alert.alert(
          "Photo access needed",
          "Chirp needs access to your photos to attach one. You can allow it in Settings.",
        );
        return;
      }

      result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ["images"],
        quality: 0.8,
        selectionLimit: 1,
      });
    } catch {
      // c139: expo-image-picker is a newly-added native module (c70) - until the EAS
      // dev build is rebuilt (c39), these calls fail at native-module resolution
      // rather than resolving to a picker. Previously this was an unhandled
      // rejection with no message: the user tapped Photo and nothing happened.
      // Matches treasurer.tsx exportCsv()'s precedent for the identical risk.
      Alert.alert(
        "Can't attach a photo yet",
        "Photo attach needs the latest app build. Rebuild the app (EAS dev build) and try again.",
      );
      return;
    }
    if (result.canceled || result.assets.length === 0) return;
    const asset = result.assets[0];

    const contentType = asset.mimeType;
    if (contentType !== "image/jpeg" && contentType !== "image/png" && contentType !== "image/webp") {
      Alert.alert("Unsupported photo", "Please choose a JPEG, PNG, or WebP image.");
      return;
    }
    if (asset.fileSize !== undefined && asset.fileSize > MAX_UPLOAD_BYTES) {
      Alert.alert("Photo too large", "Photos are limited to 10MB. Try a different one.");
      return;
    }

    setUploadingPhoto(true);
    try {
      const typedContentType = contentType as AllowedMediaContentType;
      const bytes = await (await fetch(asset.uri)).blob();
      const { upload_url, preview_url, object_name } = await getMediaUploadUrl(
        typedContentType,
        bytes.size,
      );
      await uploadMediaBytes(upload_url, bytes, typedContentType);
      setPhotoUrl(preview_url);
      setPhotoObjectName(object_name);
      setStep("compose");
    } catch (error) {
      showApiError(error, "Couldn't upload that photo");
    } finally {
      setUploadingPhoto(false);
    }
  };

  const submit = async () => {
    if (!canSubmit || postingRef.current) return;
    postingRef.current = true;
    setPosting(true);
    try {
      const media =
        photoObjectName !== null
          ? { media_object_names: [photoObjectName], post_type: "photo" as const }
          : {};
      if (chapterId !== null) {
        await createPost(chapterId, { body: body.trim(), audience: effectiveAudience, ...media });
      } else if (campusId !== null) {
        // No chapter: the campus route is the only one that will accept this, and
        // it sets audience itself, so nothing about org can be requested here.
        await createCampusPost(campusId, { body: body.trim(), ...media });
      }
    } catch (error) {
      // Body/audience deliberately survive a failure so the user can just retry.
      showApiError(error, "Couldn't post that");
      return;
    } finally {
      postingRef.current = false;
      setPosting(false);
    }
    // Deliberately outside the try: the post is already saved by here, so a
    // caller's refresh callback throwing must not be reported as a failed post
    // (and close() has already cleared the body, so the retry above is a lie).
    close();
    onPosted?.();
  };

  const campusLabel = campusName ?? "my campus";

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={close}>
      {/* c131: NOT accessibilityRole="button" — react-native-web maps that role to a
          literal HTML <button>, and this backdrop wraps every real control in the
          sheet (Post, Back, audience choices, the option rows), each of which is
          ALSO accessibilityRole="button". A <button> cannot legally contain another
          <button>; the browser's click/focus targeting on the inner ones becomes
          undefined behavior once it does. onPress alone makes the backdrop tappable
          without making it a semantic "button" - matches MediaPostCard's own
          full-screen dismiss overlay, which never had this role either. */}
      <Pressable
        onPress={close}
        style={{ flex: 1, backgroundColor: withAlpha(light.ink, 0.4), justifyContent: "flex-end" }}
      >
        {/* Inner Pressable with no onPress: swallows taps so they don't bubble to the backdrop close. */}
        <Pressable
          style={{
            backgroundColor: palette.surface,
            borderTopLeftRadius: radii.card,
            borderTopRightRadius: radii.card,
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.lg,
            paddingBottom: insets.bottom + spacing.lg,
            gap: spacing.lg,
          }}
        >
          <View
            style={{
              alignSelf: "center",
              width: 40,
              height: 4,
              borderRadius: radii.pill,
              backgroundColor: palette.border,
            }}
          />

          {/* c141: the backdrop's onPress dismisses the sheet, but c131 correctly
              took accessibilityRole="button" off it (a <button> can't legally
              contain the real buttons below it) — which also took away the ONLY
              labeled dismiss control assistive tech ever had here. Matches
              MediaPostCard's own dismiss overlay: a real, labeled control, not
              just an unlabeled tappable region. Positioned absolute so it sits in
              both steps without duplicating it in each branch below. */}
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Close"
            onPress={close}
            hitSlop={spacing.sm}
            style={{
              position: "absolute",
              top: spacing.lg,
              right: spacing.gutter,
              width: 28,
              height: 28,
              borderRadius: radii.pill,
              backgroundColor: palette.surfaceAlt,
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Feather name="x" size={16} color={palette.inkSecondary} />
          </Pressable>

          {step === "options" ? (
            <>
              <AppText variant="title">Create</AppText>
              <View>
                <ListRow
                  title="Photo"
                  subtitle={uploadingPhoto ? "Uploading..." : "Attach a photo to your post"}
                  divider
                  onPress={uploadingPhoto ? undefined : () => void pickPhoto()}
                  left={<OptionIcon icon="image" />}
                  right={uploadingPhoto ? <ActivityIndicator /> : undefined}
                />
                <ListRow
                  title="Video"
                  subtitle="Coming soon — media posting isn't wired up yet"
                  divider
                  left={<OptionIcon icon="video" muted />}
                  right={<Badge label="Soon" />}
                />
                <ListRow
                  title="Text"
                  subtitle="Post a text update"
                  divider={false}
                  onPress={() => setStep("compose")}
                  left={<OptionIcon icon="type" />}
                />
              </View>
            </>
          ) : (
            <>
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Back"
                  onPress={() => setStep("options")}
                  hitSlop={spacing.sm}
                >
                  <Feather name="chevron-left" size={22} color={palette.inkSecondary} />
                </Pressable>
                <AppText variant="title">New post</AppText>
              </View>

              {photoUrl !== null ? (
                <View style={{ borderRadius: radii.input, overflow: "hidden" }}>
                  <Image
                    source={{ uri: photoUrl }}
                    style={{ width: "100%", height: 180 }}
                    resizeMode="cover"
                  />
                  <Pressable
                    accessibilityRole="button"
                    accessibilityLabel="Remove photo"
                    onPress={() => {
                      setPhotoUrl(null);
                      setPhotoObjectName(null);
                    }}
                    hitSlop={spacing.sm}
                    style={{
                      position: "absolute",
                      top: spacing.sm,
                      right: spacing.sm,
                      width: 28,
                      height: 28,
                      borderRadius: radii.pill,
                      backgroundColor: withAlpha(light.ink, 0.6),
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    <Feather name="x" size={16} color={light.surface} />
                  </Pressable>
                </View>
              ) : null}

              <TextInput
                value={body}
                onChangeText={setBody}
                placeholder="What's going on?"
                placeholderTextColor={palette.inkFaint}
                multiline
                autoFocus
                style={{
                  ...typography.body,
                  color: palette.ink,
                  backgroundColor: palette.surfaceAlt,
                  borderRadius: radii.input,
                  paddingHorizontal: spacing.lg,
                  paddingVertical: spacing.md,
                  minHeight: 96,
                  textAlignVertical: "top",
                }}
              />

              <View style={{ gap: spacing.sm }}>
                <AppText variant="micro" tone="secondary">
                  Who can see this?
                </AppText>
                {!canChooseAudience ? (
                  // Stated, not offered. The chapter option would 403 for someone
                  // with no chapter, and a disabled control the user cannot ever
                  // enable reads as a locked feature rather than as what it is:
                  // there is one audience because they are posting as a student,
                  // not on behalf of an org.
                  <View
                    style={{
                      flexDirection: "row",
                      alignItems: "center",
                      gap: spacing.md,
                      padding: spacing.md,
                      borderRadius: radii.input,
                      backgroundColor: palette.surfaceAlt,
                      borderWidth: 1,
                      borderColor: palette.border,
                    }}
                  >
                    <Feather name="globe" size={20} color={palette.accent} />
                    <View style={{ flex: 1, gap: spacing.xs }}>
                      <AppText variant="bodyBold">{`Everyone at ${campusLabel}`}</AppText>
                      <AppText variant="caption" tone="secondary">
                        Join an org to also post privately to your chapter.
                      </AppText>
                    </View>
                  </View>
                ) : null}
                {canChooseAudience ? (
                  <AudienceChoice
                    title="My chapter only"
                    description="Private — only your chapter's members can see it."
                    selected={audience === "org"}
                    onPress={() => setAudience("org")}
                  />
                ) : null}
                {canChooseAudience ? (
                  <AudienceChoice
                    title={`Everyone at ${campusLabel}`}
                    description="Public to your whole campus — people outside your chapter can see it too."
                    selected={audience === "campus"}
                    onPress={() => setAudience("campus")}
                  />
                ) : null}
              </View>

              <Button
                label={posting ? "Posting..." : "Post"}
                onPress={() => void submit()}
                disabled={!canSubmit}
              />
            </>
          )}
        </Pressable>
      </Pressable>
    </Modal>
  );
}
