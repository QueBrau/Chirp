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
 */

import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { Image, Pressable, View, type ViewStyle } from "react-native";

import type { PostOut } from "@/api/feed";
import { cardShadow, light, radii, spacing, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { Badge } from "./Badge";
import { Chip } from "./Chip";
import { GradientAvatar } from "./GradientAvatar";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

const MEDIA_HEIGHT = 260;
const PLAY_CIRCLE = 48;
const ACTION_CHIP = 36;

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
}: MediaPostCardProps) {
  const palette = useTheme();
  const mediaUrl = post.media_urls?.[0];
  const type = mediaUrl ? post.post_type ?? "text" : "text";

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

  if (type === "text") {
    // Compact/Twitter density (§10 rule 3): tight single-line header, body, inline counts —
    // deliberately less breathing room than the photo/video cards below.
    return (
      <View style={[cardBase, { padding: spacing.md, gap: spacing.sm }]}>
        <CompactHeader name={authorName} time={timeLabel} photoUrl={authorPhotoUrl} />
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
      </View>
    );
  }

  return (
    <View style={cardBase}>
      <View style={{ height: MEDIA_HEIGHT }}>
        <Image source={{ uri: mediaUrl }} style={{ width: "100%", height: "100%" }} resizeMode="cover" />

        {/* Layered translucent-ink scrim (NOT a heavy black gradient) — carries the white author row. */}
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
        <View style={{ position: "absolute", left: spacing.lg, right: spacing.lg, bottom: spacing.lg }}>
          <AuthorRow name={authorName} time={timeLabel} photoUrl={authorPhotoUrl} onScrim />
        </View>

        {type === "video" ? (
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

        {type === "video" && post.duration_sec ? (
          <Chip
            label={formatDuration(post.duration_sec)}
            style={{ position: "absolute", top: spacing.md, right: spacing.md }}
          />
        ) : null}
      </View>

      <View style={{ padding: spacing.lg, gap: spacing.md }}>
        <AppText>{post.body}</AppText>
        {actions}
      </View>
    </View>
  );
}
