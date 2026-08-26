/**
 * MediaPostCard (DESIGN §7 FYP): renders a feed post per `post_type`.
 * - text: COMPACT per §10 rule 3 (Twitter density) — one tight header row
 *   (small avatar + name + time inline), body, inline icon+count actions.
 *   No 36px action chips here; that breathing-room treatment is reserved for
 *   media so the two densities read as deliberately different, not sloppy.
 * - photo: full-bleed image (radius 20, height ~260) with a layered
 *   translucent-ink scrim (NOT a heavy black gradient) carrying a white
 *   author row; caption sits below the media, inside the card.
 * - video: photo layout + centered Feather play in a translucent 48 circle +
 *   a static duration Chip top-right. Mock: thumbnail only, no playback.
 * Action row (photo/video only): 36 circular surfaceAlt chips (Feather heart /
 * message-circle / send) with a count Badge attached; active state =
 * accentSoft chip + accent icon.
 *
 * Overflow control (board c35, App Store Guideline 1.2): a discreet
 * more-horizontal icon in the header row of every variant, offering Report
 * and (unless `canBlock` is false) Block — mirrors the Chirps board's own
 * report/block affordance (app/(tabs)/chirps/index.tsx), rolled locally into
 * this component rather than the screen since every post here has a KNOWN
 * author (`authorName`/`post.author_id`), unlike Chirp where the client never
 * learns who posted. The sheet/report-reasons Modal is only ever mounted
 * while open for THIS card (`sheet !== null`), not one-per-card-always, so a
 * long feed doesn't carry N idle Modals. The screen owns the actual API
 * calls and any post-block local-state cleanup via `onReport`/`onBlock`.
 */

import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useEffect, useState } from "react";
import { Image, Modal, Pressable, View, type ViewStyle } from "react-native";

import type { PostOut } from "@/api/feed";
import { confirmAction } from "@/lib/alert";
import { cardShadow, light, radii, spacing, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { Badge } from "./Badge";
import { Chip } from "./Chip";
import { GradientAvatar } from "./GradientAvatar";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

const MEDIA_HEIGHT = 260;
const PLAY_CIRCLE = 48;
const ACTION_CHIP = 36;

/** Preset report reasons (backend requires a non-empty `reason` string) — same
 * three presets as the Chirps board's report sheet. */
const REPORT_REASONS = ["Spam", "Harassment", "Inappropriate"];

/**
 * One choice in the action sheet below. Rolled by hand instead of using
 * `Alert` for the menus because Android's Alert supports AT MOST three
 * buttons — the report-reason list (three reasons + Cancel) would silently
 * lose an option there, the exact bug the Chirp screen hit first. Alert is
 * still fine for the block confirm/error dialogs below, which never exceed
 * three buttons.
 */
interface SheetOption {
  label: string;
  destructive?: boolean;
  onPress: () => void;
}

/** Discreet overflow trigger — as unobtrusive as the timestamp it sits next to. */
function OverflowButton({ onPress, onScrim = false }: { onPress: () => void; onScrim?: boolean }) {
  const palette = useTheme();
  const color = onScrim ? withAlpha(palette.onAccent, 0.85) : palette.inkFaint;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="More options"
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
    >
      <Feather name="more-horizontal" size={16} color={color} />
    </Pressable>
  );
}

export interface MediaPostCardProps {
  post: PostOut;
  authorName: string;
  /** Mock photo (§10.2), e.g. `https://i.pravatar.cc/150?u=<id>` — falls back to the initials gradient. */
  authorPhotoUrl?: string | null;
  /** Precomputed relative-age label (e.g. "5m", "3h") — screen owns time formatting. */
  timeLabel: string;
  likeCount: number;
  commentCount: number;
  likedByMe: boolean;
  onToggleLike: () => void;
  /** Overflow menu -> Report -> reason picked. The screen performs the actual
   * createReport({ target_type: "post", target_id: post.id, reason }) call. */
  onReport: (reason: string) => void;
  /** Overflow menu -> Block, after the caller confirms (this component owns
   * that confirm, since it's the one that knows `authorName` to put in the
   * copy). The screen performs blockUser(post.author_id) and the resulting
   * local-state/refetch cleanup. */
  onBlock: () => void;
  /** False for the signed-in user's own posts — Block never applies to yourself. */
  canBlock: boolean;
}

/** Deterministic cosmetic count for the "send" chip — mock only, no share tracking yet. */
function mockShareCount(seed: string): number {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) | 0;
  return (Math.abs(hash) % 9) + 1;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function ActionChip({
  icon,
  count,
  active = false,
  label,
  onPress,
}: {
  icon: FeatherIconName;
  count: number;
  active?: boolean;
  label: string;
  onPress?: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={onPress}
      hitSlop={spacing.xs}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.xs,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <View
        style={{
          width: ACTION_CHIP,
          height: ACTION_CHIP,
          borderRadius: radii.pill,
          backgroundColor: active ? palette.accentSoft : palette.surfaceAlt,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Feather name={icon} size={18} color={active ? palette.accent : palette.inkSecondary} />
      </View>
      <Badge label={String(count)} tone={active ? "accent" : "neutral"} />
    </Pressable>
  );
}

function AuthorRow({
  name,
  time,
  photoUrl,
  onScrim = false,
}: {
  name: string;
  time: string;
  photoUrl?: string | null;
  onScrim?: boolean;
}) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
      <GradientAvatar name={name} size={32} photoUrl={photoUrl} />
      <View style={{ gap: 2 }}>
        <AppText variant="headline" tone={onScrim ? "onAccent" : "primary"} numberOfLines={1}>
          {name}
        </AppText>
        <AppText variant="caption" tone={onScrim ? "onAccent" : "tertiary"}>
          {time}
        </AppText>
      </View>
    </View>
  );
}

/** Tight single-line header for the compact text-post density (§10 rule 3): small
 * avatar + name + time all inline, instead of the two-line stacked AuthorRow. */
function CompactHeader({ name, time, photoUrl }: { name: string; time: string; photoUrl?: string | null }) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
      <GradientAvatar name={name} size={28} photoUrl={photoUrl} />
      <View style={{ flexDirection: "row", alignItems: "baseline", gap: spacing.xs, flexShrink: 1 }}>
        <AppText variant="bodyBold" numberOfLines={1}>
          {name}
        </AppText>
        <AppText variant="caption" tone="tertiary" numberOfLines={1}>
          · {time}
        </AppText>
      </View>
    </View>
  );
}

/** Inline icon + tabular count for the compact text-post action row (§10 rule 3/6) —
 * Twitter-density, no 36px chip circle (that's the media variant's breathing room). */
function InlineAction({
  icon,
  count,
  active = false,
  label,
  onPress,
}: {
  icon: FeatherIconName;
  count: number;
  active?: boolean;
  label: string;
  onPress?: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={onPress}
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.xs,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather name={icon} size={15} color={active ? palette.accent : palette.inkFaint} />
      <AppText
        variant="caption"
        tone={active ? "accent" : "tertiary"}
        style={{ fontVariant: ["tabular-nums"] }}
      >
        {count}
      </AppText>
    </Pressable>
  );
}

export function MediaPostCard({
  post,
  authorName,
  authorPhotoUrl,
  timeLabel,
  likeCount,
  commentCount,
  likedByMe,
  onToggleLike,
  onReport,
  onBlock,
  canBlock,
}: MediaPostCardProps) {
  const palette = useTheme();
  const mediaUrl = post.media_urls?.[0];
  const type = mediaUrl ? post.post_type ?? "text" : "text";
  const [sheet, setSheet] = useState<{ title: string; options: SheetOption[] } | null>(null);
  // Media that failed to load (board c140). Before this existed, a photo the device
  // could not fetch rendered as an EMPTY BOX with the author row floating over nothing —
  // no error, no retry, nothing to tell the user or us that anything went wrong. That
  // matters much more now that media urls are capability urls with a finite life: an
  // expired one 403s, and silence is the worst possible response to a state we know how
  // to explain.
  const [mediaFailed, setMediaFailed] = useState(false);
  // Reset per url, not per mount. These cards are re-rendered constantly (the feed
  // replaces every post object on each load and this component is not memoized), and a
  // capability url legitimately changes when its signing window rolls over — a stale
  // `true` would keep showing the fallback over a url that now works perfectly.
  useEffect(() => setMediaFailed(false), [mediaUrl]);

  const openReportReasons = () => {
    setSheet({
      title: "What's wrong with it?",
      options: REPORT_REASONS.map((reason) => ({
        label: reason,
        onPress: () => onReport(reason),
      })),
    });
  };

  const confirmBlock = () => {
    // Unlike Chirp (client genuinely doesn't know the author), a feed post carries
    // a known author_id/display_name — the copy can and should name them.
    confirmAction({
      title: `Block ${authorName}?`,
      message: `You won't see posts from ${authorName} again.`,
      confirmLabel: "Block",
      destructive: true,
      order: "confirm-first",
      onConfirm: onBlock,
    });
  };

  const openMenu = () => {
    const options: SheetOption[] = [{ label: "Report", onPress: openReportReasons }];
    if (canBlock) {
      options.push({ label: "Block", destructive: true, onPress: confirmBlock });
    }
    setSheet({ title: "More options", options });
  };

  const cardBase: ViewStyle = {
    backgroundColor: palette.surface,
    borderRadius: radii.card,
    borderWidth: 1,
    borderColor: palette.border,
    overflow: "hidden",
    ...cardShadow(palette),
  };

  const actions = (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
      <ActionChip
        icon="heart"
        count={likeCount}
        active={likedByMe}
        label={likedByMe ? "Unlike" : "Like"}
        onPress={onToggleLike}
      />
      <ActionChip icon="message-circle" count={commentCount} label="Comment" />
      <ActionChip icon="send" count={mockShareCount(post.id)} label="Send" />
    </View>
  );

  // Action sheet Modal shared by every variant below — only mounted while
  // THIS card's menu is actually open (`sheet !== null`), not one idle Modal
  // per card in a long feed.
  const sheetModal =
    sheet !== null ? (
      <Modal transparent visible animationType="fade" onRequestClose={() => setSheet(null)}>
        <Pressable
          onPress={() => setSheet(null)}
          style={{
            flex: 1,
            justifyContent: "flex-end",
            backgroundColor: "rgba(16,18,35,0.45)",
            padding: spacing.md,
          }}
        >
          <View style={{ backgroundColor: palette.surface, borderRadius: radii.card, overflow: "hidden" }}>
            <AppText
              variant="micro"
              tone="tertiary"
              style={{ padding: spacing.md, paddingBottom: spacing.sm }}
            >
              {sheet.title.toUpperCase()}
            </AppText>
            {sheet.options.map((option) => (
              <Pressable
                key={option.label}
                accessibilityRole="button"
                onPress={() => {
                  // Close first; an option that opens a follow-up sheet (Report)
                  // re-sets state in the same batch, so its sheet wins.
                  setSheet(null);
                  option.onPress();
                }}
                style={({ pressed }) => ({
                  padding: spacing.md,
                  borderTopWidth: 1,
                  borderTopColor: palette.border,
                  backgroundColor: pressed ? palette.surfaceAlt : "transparent",
                })}
              >
                <AppText tone={option.destructive ? "danger" : "primary"}>{option.label}</AppText>
              </Pressable>
            ))}
          </View>
          <Pressable
            accessibilityRole="button"
            onPress={() => setSheet(null)}
            style={({ pressed }) => ({
              marginTop: spacing.sm,
              padding: spacing.md,
              alignItems: "center",
              backgroundColor: palette.surface,
              borderRadius: radii.card,
              opacity: pressed ? 0.8 : 1,
            })}
          >
            <AppText>Cancel</AppText>
          </Pressable>
        </Pressable>
      </Modal>
    ) : null;

  if (type === "text") {
    // Compact/Twitter density (§10 rule 3): tight single-line header, body, inline counts —
    // deliberately less breathing room than the photo/video cards below.
    return (
      <View style={[cardBase, { padding: spacing.md, gap: spacing.sm }]}>
        <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
          <View style={{ flexShrink: 1 }}>
            <CompactHeader name={authorName} time={timeLabel} photoUrl={authorPhotoUrl} />
          </View>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            {/* Tier indicator (board c102): a viewer only ever receives this post
                at all if they're active, so the badge is purely informative — it
                tells them WHY this post reads differently, not a gate. */}
            {post.audience === "org_actives" ? <Chip label="Actives only" variant="accent" /> : null}
            <OverflowButton onPress={openMenu} />
          </View>
        </View>
        <AppText>{post.body}</AppText>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
          <InlineAction
            icon="heart"
            count={likeCount}
            active={likedByMe}
            label={likedByMe ? "Unlike" : "Like"}
            onPress={onToggleLike}
          />
          <InlineAction icon="message-circle" count={commentCount} label="Comment" />
          <InlineAction icon="send" count={mockShareCount(post.id)} label="Send" />
        </View>
        {sheetModal}
      </View>
    );
  }

  return (
    <View style={cardBase}>
      <View style={{ height: MEDIA_HEIGHT }}>
        {mediaFailed ? (
          /* Unavailable-photo state. Deliberately NOT the scrim treatment: the scrim
             exists to float white text over a photo, and with no photo underneath it
             would put low-contrast white on a pale surface. So this state drops the
             scrim entirely and switches the author row back to normal ink tones
             (`onScrim` below follows `!mediaFailed` for exactly that reason). */
          <View
            style={{
              width: "100%",
              height: "100%",
              alignItems: "center",
              justifyContent: "center",
              gap: spacing.sm,
              backgroundColor: palette.surfaceAlt,
            }}
          >
            <Feather name="image" size={22} color={palette.inkFaint} />
            <AppText variant="caption" tone="tertiary">
              Photo unavailable
            </AppText>
          </View>
        ) : (
          <Image
            source={{ uri: mediaUrl }}
            style={{ width: "100%", height: "100%" }}
            resizeMode="cover"
            onError={() => setMediaFailed(true)}
          />
        )}

        {/* Layered translucent-ink scrim (NOT a heavy black gradient) — carries the white author row. */}
        {!mediaFailed ? (
          <>
            <View
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                bottom: 0,
                height: 132,
                backgroundColor: withAlpha(light.ink, 0.22),
              }}
            />
            <View
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                bottom: 0,
                height: 76,
                backgroundColor: withAlpha(light.ink, 0.34),
              }}
            />
          </>
        ) : null}
        <View
          style={{
            position: "absolute",
            left: spacing.lg,
            right: spacing.lg,
            bottom: spacing.lg,
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <View style={{ flexShrink: 1 }}>
            <AuthorRow
              name={authorName}
              time={timeLabel}
              photoUrl={authorPhotoUrl}
              onScrim={!mediaFailed}
            />
          </View>
          <OverflowButton onPress={openMenu} onScrim={!mediaFailed} />
        </View>

        {type === "video" && !mediaFailed ? (
          <View
            pointerEvents="none"
            style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center" }}
          >
            <View
              style={{
                width: PLAY_CIRCLE,
                height: PLAY_CIRCLE,
                borderRadius: radii.pill,
                backgroundColor: withAlpha(palette.onAccent, 0.3),
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Feather name="play" size={22} color={palette.onAccent} />
            </View>
          </View>
        ) : null}

        {type === "video" && post.duration_sec && !mediaFailed ? (
          <Chip
            label={formatDuration(post.duration_sec)}
            style={{ position: "absolute", top: spacing.md, right: spacing.md }}
          />
        ) : null}

        {/* Tier indicator (board c102), top-left mirroring the duration Chip's
            top-right — purely informative, see the text-variant comment above. */}
        {post.audience === "org_actives" ? (
          <Chip
            label="Actives only"
            variant="accent"
            style={{ position: "absolute", top: spacing.md, left: spacing.md }}
          />
        ) : null}
      </View>

      <View style={{ padding: spacing.lg, gap: spacing.md }}>
        <AppText>{post.body}</AppText>
        {actions}
      </View>
      {sheetModal}
    </View>
  );
}
