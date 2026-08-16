/**
 * CreateSheet (DESIGN §7/§8.7): bottom sheet opened by the Home/Org FAB —
 * Photo / Video / Text rows. Photo and Video are visibly disabled: there is
 * no media upload capability anywhere in the stack (media_urls is a column
 * on the post model, but no endpoint, storage bucket, or presign flow exists
 * to turn a picked photo/video into a URL), so faking a picker would just
 * silently drop the media. Text opens a real composer — body + an audience
 * picker (org/campus, board Decisions log Aug 14) — wired to the real
 * POST /chapters/{chapter_id}/posts.
 */

import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useRef, useState } from "react";
import { Alert, Modal, Pressable, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { ApiError } from "@/api/client";
import { createPost, type PostAudience } from "@/api/feed";
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
   * Chapter to post into — posts always belong to a chapter (posts.chapter_id
   * is NOT NULL, routers/feed.py); `audience` only controls who can see it.
   * null means the caller has no active chapter membership. Fab already keeps
   * the sheet from being opened in that case (there is no chapter to post
   * into), but submit() guards against it again rather than trusting the caller.
   */
  chapterId: string | null;
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

export function CreateSheet({ visible, onClose, chapterId, campusName, onPosted }: CreateSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [step, setStep] = useState<Step>("options");
  const [body, setBody] = useState("");
  // Never default to campus (board Decisions log, Aug 14 — mirrors the
  // server-side PostCreate.audience default): a user who never touches this
  // control must not accidentally broadcast a chapter post campus-wide.
  const [audience, setAudience] = useState<PostAudience>("org");
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
  };

  const close = () => {
    reset();
    onClose();
  };

  const canSubmit = body.trim().length > 0 && chapterId !== null && !posting;

  const submit = async () => {
    if (!canSubmit || chapterId === null || postingRef.current) return;
    postingRef.current = true;
    setPosting(true);
    try {
      await createPost(chapterId, { body: body.trim(), audience });
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
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Close"
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

          {step === "options" ? (
            <>
              <AppText variant="title">Create</AppText>
              <View>
                <ListRow
                  title="Photo"
                  subtitle="Coming soon — media posting isn't wired up yet"
                  divider
                  left={<OptionIcon icon="image" muted />}
                  right={<Badge label="Soon" />}
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
                <AudienceChoice
                  title="My chapter only"
                  description="Private — only your chapter's members can see it."
                  selected={audience === "org"}
                  onPress={() => setAudience("org")}
                />
                <AudienceChoice
                  title={`Everyone at ${campusLabel}`}
                  description="Public to your whole campus — people outside your chapter can see it too."
                  selected={audience === "campus"}
                  onPress={() => setAudience("campus")}
                />
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
