/**
 * MediaPostCard (DESIGN §5 TintedPostCard, §7 FYP): the one card every post wears,
 * on Home, on Chirps' neighbour surfaces and in an org's own feed.
 *
 * ONE STRUCTURE FOR EVERY POST TYPE since c383 (braul, Sep 7, from a reference shot
 * he supplied for Home and Chirps): rotating pastel tint, header row (avatar + name
 * over time, with a circular overflow control at the right end), body, an INSET
 * media block for photo/video, then the action row. `post_type` now changes only
 * whether that media block is present and what floats on it, never the chrome.
 *
 * WHAT THAT REPLACED, and why the deletions are the good part. Media posts used to
 * be full-bleed with the author row floated over the photo on a translucent ink
 * scrim. That scrim was the sole reason for two awkward code paths, and both are
 * gone with it:
 *   - the `onScrim` tone flag threaded through AuthorRow and OverflowButton, which
 *     existed to turn text white over a photo;
 *   - the separate unavailable-media tone branch (c140) - a failed image left white
 *     scrim text on a pale surface, so the fallback had to switch tones back. An
 *     unavailable photo is now just an inset surfaceAlt block, and the header above
 *     it never changed tone in the first place.
 * Density contrast (§10.3) survives as padding and gaps only; the rotating tint is
 * what now keeps a scrolling column from reading as identical rectangles.
 *
 * Action row: Feather icon + a caption label, no chip circle. The label is THE COUNT
 * WHEN THERE IS ONE and the action's name when there is not - "Like" rather than a
 * meaningless "0". The accessibility label is always the full action name, never the
 * digits. Only an ACTIVE HEART becomes the filled shape in `like` red (c222/c229,
 * carried over unchanged and still keyed on the ICON, not on `active` alone, so a
 * future active comment or send chip cannot quietly turn red).
 *
 * Comments (board c228): the message-circle opens CommentsSheet on this card. It had
 * carried a real count, a button role and a "Comment" label with no onPress since the
 * FYP landed, which is worse than no control at all. A sheet rather than a post-detail
 * route, because this component renders from two screens with separate navigation and
 * a sheet needs neither of them to change.
 *
 * Overflow control (board c35, App Store Guideline 1.2): the circular button in the
 * header row offers Report and (unless `canBlock` is false) Block — mirrors the Chirps
 * board's own report/block affordance (app/(tabs)/chirps/index.tsx), rolled locally
 * into this component rather than the screen since every post here has a KNOWN author
 * (`authorName`/`post.author_id`), unlike a chirp where the client never learns who
 * posted. The sheet/report-reasons Modal is only ever mounted while open for THIS card
 * (`sheet !== null`), not one-per-card-always, so a long feed doesn't carry N idle
 * Modals. The screen owns the actual API calls and any post-block local-state cleanup
 * via `onReport`/`onBlock`.
 */

import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useEffect, useState } from "react";
import { Image, Modal, Pressable, View, type ViewStyle } from "react-native";

import type { PostOut } from "@/api/feed";
import { confirmAction } from "@/lib/alert";
import { cardShadow, metrics, onTintControl, postTint, radii, spacing, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { Chip } from "./Chip";
import { CommentsSheet } from "./CommentsSheet";
import { FilledHeart } from "./FilledHeart";
import { GradientAvatar } from "./GradientAvatar";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

const MEDIA_HEIGHT = 240;
const PLAY_CIRCLE = 48;
const AVATAR = 40;
/** Shared with the Chirps board's own overflow button so the two cards cannot drift. */
const OVERFLOW_CIRCLE = metrics.tintControlSize;

/** iOS HIG / WCAG 2.5.5 minimum tappable size. */
const TOUCH_TARGET = 44;
/**
 * Both hit slops below are DERIVED from the control they pad, never hand-picked -
 * that is the whole lesson of c307, where an inline action's hand-picked slop of 8
 * left a 33pt target next to a sibling that met 44. Sized off the taller of the icon
 * and its caption in each case, so the arithmetic stays honest if either metric moves.
 */
const ACTION_ICON = 18;
const ACTION_HIT_SLOP = Math.ceil((TOUCH_TARGET - ACTION_ICON) / 2);
const OVERFLOW_HIT_SLOP = Math.ceil((TOUCH_TARGET - OVERFLOW_CIRCLE) / 2);

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

/**
 * Overflow trigger: the glyph in a circular soft control (DESIGN §5 TintedPostCard).
 * `onTintControl` rather than `surface` because the dark tints sit LIGHTER than
 * `surface` — see the token's own note.
 */
function OverflowButton({ onPress }: { onPress: () => void }) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="More options"
      onPress={onPress}
      hitSlop={OVERFLOW_HIT_SLOP}
      style={({ pressed }) => ({
        width: OVERFLOW_CIRCLE,
        height: OVERFLOW_CIRCLE,
        borderRadius: radii.pill,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: onTintControl(palette),
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather name="more-horizontal" size={16} color={palette.inkSecondary} />
    </Pressable>
  );
}

export interface MediaPostCardProps {
  post: PostOut;
  authorName: string;
  /** Mock photo (§10.2), e.g. `https://i.pravatar.cc/150?u=<id>` — falls back to the initials gradient. */
  authorPhotoUrl?: string | null;
  /**
   * This card's position in its list, which picks the rotating tint (§5). REQUIRED
   * rather than defaulted: a defaulted 0 would give a new call site a column of
   * identically tinted cards and look deliberate, so the type makes you say it.
   */
  tintIndex: number;
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

/** Deterministic cosmetic count for the "send" action — mock only, no share tracking yet. */
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

/**
 * One action in the card's bottom row (§7): icon beside a caption label, where the
 * label is the count if there is one and `word` if there is not. `label` is what a
 * screen reader says and is always the action's name — a button that announces "12"
 * tells you nothing about what pressing it does.
 */
function PostAction({
  icon,
  count,
  word,
  label,
  active = false,
  onPress,
}: {
  icon: FeatherIconName;
  count: number;
  word: string;
  label: string;
  active?: boolean;
  onPress?: () => void;
}) {
  const palette = useTheme();
  // c222/c229, unchanged: keyed on the ICON, not on `active` alone. This row is shared
  // by heart, message-circle and send, and only the heart ever passes active today —
  // keying on active alone would work now and quietly turn a future active comment or
  // send red. The INACTIVE heart also stays on the Feather glyph, which is the point:
  // an unliked post must look exactly as it always did.
  const activeHeart = active && icon === "heart";
  const color = activeHeart ? palette.like : active ? palette.accent : palette.inkSecondary;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={onPress}
      hitSlop={ACTION_HIT_SLOP}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.sm,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      {activeHeart ? (
        <FilledHeart size={ACTION_ICON} color={palette.like} />
      ) : (
        <Feather name={icon} size={ACTION_ICON} color={color} />
      )}
      <AppText variant="caption" style={{ color, fontVariant: ["tabular-nums"] }}>
        {count > 0 ? String(count) : word}
      </AppText>
    </Pressable>
  );
}

/** Header row of every variant (§5): avatar + name over time. */
function AuthorRow({
  name,
  time,
  photoUrl,
}: {
  name: string;
  time: string;
  photoUrl?: string | null;
}) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
      <GradientAvatar name={name} size={AVATAR} photoUrl={photoUrl} />
      <View style={{ gap: 2, flexShrink: 1 }}>
        <AppText variant="headline" numberOfLines={1}>
          {name}
        </AppText>
        <AppText variant="caption" tone="tertiary">
          {time}
        </AppText>
      </View>
    </View>
  );
}

export function MediaPostCard({
  post,
  authorName,
  authorPhotoUrl,
  tintIndex,
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
  const hasMedia = type !== "text";
  const [sheet, setSheet] = useState<{ title: string; options: SheetOption[] } | null>(null);
  // Media that failed to load (board c140). Before this existed, a photo the device
  // could not fetch rendered as an EMPTY BOX — no error, no retry, nothing to tell the
  // user or us that anything went wrong. That matters much more now that media urls are
  // capability urls with a finite life: an expired one 403s, and silence is the worst
  // possible response to a state we know how to explain.
  const [mediaFailed, setMediaFailed] = useState(false);
  // Reset per url, not per mount. These cards are re-rendered constantly (the feed
  // replaces every post object on each load and this component is not memoized), and a
  // capability url legitimately changes when its signing window rolls over — a stale
  // `true` would keep showing the fallback over a url that now works perfectly.
  useEffect(() => setMediaFailed(false), [mediaUrl]);

  // c228: the comments sheet, mounted only while THIS card's thread is open, for the
  // same reason the report/block Modal above is - a long feed must not carry N idle
  // Modals.
  const [commentsOpen, setCommentsOpen] = useState(false);
  // The thread's real length once the sheet has loaded it, else null. Preferred over
  // the `commentCount` prop because both numbers are produced by the same server-side
  // rule (c109: comment_count and list_comments apply the identical blocked-author
  // filter), so the loaded rows ARE the count - and the row can then never read "3"
  // over a sheet showing two.
  const [loadedCommentCount, setLoadedCommentCount] = useState<number | null>(null);
  // Drop the local number whenever the screen hands down a fresh one, same
  // reset-on-input-change shape as mediaFailed above. Server truth is never older than
  // what we cached from it, so a refetch (after a block, or anyone else's comment) must
  // not lose to a stale local count.
  useEffect(() => setLoadedCommentCount(null), [commentCount]);
  const shownCommentCount = loadedCommentCount ?? commentCount;

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
    backgroundColor: postTint(palette, tintIndex),
    borderRadius: radii.card,
    borderWidth: 1,
    borderColor: palette.border,
    overflow: "hidden",
    padding: spacing.lg,
    // §10.3 as narrowed by c383: the chrome is identical for both densities now, so
    // the contrast between a compact text post and a breathing media one lives here
    // and in the media block's own height. Nowhere else.
    gap: hasMedia ? spacing.md : spacing.sm,
    ...cardShadow(palette),
  };

  // Action sheet Modal — only mounted while THIS card's menu is actually open
  // (`sheet !== null`), not one idle Modal per card in a long feed.
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

  // c228: the other sheet this card owns. It is what makes the comment action work
  // identically from the Home feed and the org Feed segment without either screen
  // learning a new route.
  const commentsModal = commentsOpen ? (
    <CommentsSheet
      postId={post.id}
      onClose={() => setCommentsOpen(false)}
      onCountChange={setLoadedCommentCount}
    />
  ) : null;

  return (
    <View style={cardBase}>
      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm }}>
        {/* flexShrink so a long display name ellipsizes rather than pushing the control
            off the card; the control itself never shrinks. */}
        <View style={{ flexShrink: 1 }}>
          <AuthorRow name={authorName} time={timeLabel} photoUrl={authorPhotoUrl} />
        </View>
        <OverflowButton onPress={openMenu} />
      </View>

      {/* Tier indicator (board c102): a viewer only ever receives this post at all if
          they're active, so the badge is purely informative — it tells them WHY this
          post reads differently, not a gate.
          ON ITS OWN ROW, not in the header beside the overflow button, and that is a
          real fix rather than a preference: at 375pt the chip plus a 40 avatar plus the
          32 control left so little room that `numberOfLines={1}` truncated a perfectly
          ordinary name to "Devon Cl...". Caught by rendering it, not by reading it. The
          author's name is the one thing on this card that must never be abbreviated to
          make space for chrome. */}
      {post.audience === "org_actives" ? (
        <View style={{ flexDirection: "row" }}>
          <Chip label="Actives only" variant="accent" />
        </View>
      ) : null}

      <AppText>{post.body}</AppText>

      {hasMedia ? (
        <View style={{ height: MEDIA_HEIGHT, borderRadius: radii.media, overflow: "hidden" }}>
          {mediaFailed ? (
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

          {type === "video" && !mediaFailed ? (
            <View
              pointerEvents="none"
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                right: 0,
                bottom: 0,
                alignItems: "center",
                justifyContent: "center",
              }}
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
        </View>
      ) : null}

      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xl }}>
        <PostAction
          icon="heart"
          count={likeCount}
          word="Like"
          active={likedByMe}
          label={likedByMe ? "Unlike" : "Like"}
          onPress={onToggleLike}
        />
        <PostAction
          icon="message-circle"
          count={shownCommentCount}
          word="Comment"
          label="Comment"
          onPress={() => setCommentsOpen(true)}
        />
        <PostAction icon="send" count={mockShareCount(post.id)} word="Send" label="Send" />
      </View>

      {sheetModal}
      {commentsModal}
    </View>
  );
}
