/**
 * CommentsSheet (board c228): the thread behind a post's comment chip.
 *
 * The chip had rendered a real count, an accessibilityRole="button" and an
 * accessibilityLabel="Comment" since the FYP landed, and no onPress — so it read as a
 * working control to sighted users and announced itself as one to VoiceOver, and was
 * neither. This is that control's other half.
 *
 * A SHEET, NOT A ROUTED POST-DETAIL SCREEN (braul's call, board c228). MediaPostCard
 * renders from two places that own their own navigation — app/(tabs)/feed/index.tsx
 * and app/(tabs)/chapter/index.tsx — and a route would have to exist under both, or
 * under a shared stack neither has. A sheet the card owns works identically from both
 * with no routing change at all, and it is the same move the report/block Modal in
 * MediaPostCard already made for the same reason.
 *
 * Structure follows CreateSheet/CreateEventSheet, including their two hard-won
 * accessibility fixes:
 *   - c131: the backdrop takes onPress and NOT accessibilityRole="button".
 *     react-native-web turns that role into a literal <button>, and this backdrop
 *     wraps the send button and the close button, which are both real buttons.
 *   - c141: which leaves assistive tech with no labeled way out, so there is an
 *     explicit, labeled Close control rather than only a tappable region.
 *
 * Mounted by MediaPostCard only while open, never one-per-card-always — same reasoning
 * as the report/block Modal it sits beside, and the reason this loads on mount rather
 * than on a `visible` prop flipping true.
 *
 * NO autoFocus on the composer, deliberately unlike CreateSheet's. That sheet exists
 * to write; this one is opened to READ a thread, and popping the keyboard would cover
 * the thread with the thing the user did not ask for yet.
 */

import { Feather } from "@expo/vector-icons";
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Modal, Pressable, ScrollView, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { createComment, listComments, type PostCommentOut } from "@/api/feed";
import { showApiError } from "@/lib/alert";
import { isOverLimit, MAX_COMMENT_BODY_LENGTH } from "@/lib/contentLimits";
import { compactAge as age } from "@/lib/dates";
import { inputField, light, radii, spacing, useTheme, withAlpha } from "@/theme";

import { AppText } from "./AppText";
import { CharCounter } from "./CharCounter";
import { EmptyState } from "./EmptyState";
import { GradientAvatar } from "./GradientAvatar";

/** A failed load is its own state, never an empty list (the mistake feed/index.tsx's
 * own LoadState comment records: a silent [] hid a broken fetch for a week). */
type LoadState = "loading" | "loaded" | "error";

function CommentRow({ comment }: { comment: PostCommentOut }) {
  return (
    <View style={{ flexDirection: "row", gap: spacing.md }}>
      {/* display_name is non-null server-side (c228's join), so this never falls back
          to the "?" initials GradientAvatar renders for an empty name. */}
      <GradientAvatar name={comment.display_name} size={32} photoUrl={comment.avatar_url} />
      <View style={{ flex: 1, gap: 2 }}>
        <View style={{ flexDirection: "row", alignItems: "baseline", gap: spacing.xs }}>
          <AppText variant="bodyBold" numberOfLines={1} style={{ flexShrink: 1 }}>
            {comment.display_name}
          </AppText>
          <AppText variant="caption" tone="tertiary">
            · {age(comment.created_at)}
          </AppText>
        </View>
        <AppText>{comment.body}</AppText>
      </View>
    </View>
  );
}

export interface CommentsSheetProps {
  postId: string;
  onClose: () => void;
  /**
   * The thread's length, reported on every load and after every successful send.
   *
   * This is what keeps the card's chip and this sheet from ever disagreeing. Both
   * numbers come from the same server-side rule: _post_counts_select's comment_count
   * and list_comments apply the identical blocked-author filter (c109), and
   * list_comments is unpaginated, so the rows rendered here ARE the count. The card
   * shows this number instead of its own stale prop once it has one, rather than
   * incrementing a local counter and hoping the two stay in step.
   */
  onCountChange: (count: number) => void;
}

export function CommentsSheet({ postId, onClose, onCountChange }: CommentsSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [comments, setComments] = useState<PostCommentOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  // Same hard guard CreateSheet's submit uses: a ref is read and written
  // synchronously, so two taps landing inside one render can't both get through the
  // way a `disabled` prop one render behind would let them.
  const sendingRef = useRef(false);

  const load = async () => {
    setLoadState("loading");
    try {
      const rows = await listComments(postId);
      setComments(rows);
      setLoadState("loaded");
      onCountChange(rows.length);
    } catch {
      setLoadState("error");
    }
  };

  useEffect(() => {
    void load();
    // Mounted only while open and only for one post, so this runs exactly once per
    // opening. postId cannot change under a mounted sheet.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [postId]);

  const canSend =
    draft.trim().length > 0 && !isOverLimit(draft, MAX_COMMENT_BODY_LENGTH) && !sending;

  const send = async () => {
    if (!canSend || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    try {
      const created = await createComment(postId, { body: draft.trim() });
      // Appended rather than refetched: list_comments orders oldest-first, so a new
      // comment belongs exactly here, and the POST response is the same shape the
      // list returns (pinned by a backend test) including the author's name.
      setComments((current) => {
        const next = [...current, created];
        onCountChange(next.length);
        return next;
      });
      setDraft("");
      // A thread that failed to load and then got a comment sent into it is no longer
      // in an error state - the send proves the post is readable and the one row we
      // now hold is real. Leaving it on "error" would hide the comment just written.
      setLoadState("loaded");
    } catch (error) {
      // The draft deliberately survives: same reasoning as CreateSheet's failed post,
      // where clearing the body turns "try again" into a lie.
      showApiError(error, "Couldn't post that comment");
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  return (
    <Modal transparent visible animationType="slide" onRequestClose={onClose}>
      {/* c131: onPress only, no accessibilityRole - see the file header. */}
      <Pressable
        onPress={onClose}
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
            maxHeight: "80%",
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

          {/* c141: the labeled dismiss control the backdrop cannot be. */}
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Close"
            onPress={onClose}
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
              zIndex: 1,
            }}
          >
            <Feather name="x" size={16} color={palette.inkSecondary} />
          </Pressable>

          <AppText variant="title">
            {loadState === "loaded" && comments.length > 0 ? `Comments (${comments.length})` : "Comments"}
          </AppText>

          {loadState === "loading" ? (
            <View style={{ paddingVertical: spacing.xxl, alignItems: "center" }}>
              <ActivityIndicator color={palette.accent} />
            </View>
          ) : loadState === "error" ? (
            // Retryable in place, matching feed/index.tsx's own failed-load treatment.
            // A sheet is cheap to reopen, but making someone close and re-tap to retry
            // is asking them to guess that reopening IS the retry.
            <EmptyState
              title="Couldn't load the comments"
              message="Check your connection and try again."
              actionLabel="Try again"
              onAction={() => void load()}
            />
          ) : comments.length === 0 ? (
            // Not a dead end: the composer below stays mounted in this state, so the
            // empty thread is an invitation with the control to act on it right there,
            // which is why this one carries no action button of its own.
            <EmptyState title="No comments yet" message="Be the first to say something." />
          ) : (
            <ScrollView
              showsVerticalScrollIndicator={false}
              contentContainerStyle={{ gap: spacing.lg, paddingBottom: spacing.xs }}
            >
              {comments.map((comment) => (
                <CommentRow key={comment.id} comment={comment} />
              ))}
            </ScrollView>
          )}

          {/* Pill input + accent circular send, per DESIGN.md's composer treatment. */}
          <View style={{ flexDirection: "row", alignItems: "flex-end", gap: spacing.sm }}>
            <TextInput
              value={draft}
              onChangeText={setDraft}
              placeholder="Add a comment"
              placeholderTextColor={palette.inkFaint}
              multiline
              style={{ flex: 1, ...inputField(palette), maxHeight: 96 }}
            />
            <CharCounter value={draft} limit={MAX_COMMENT_BODY_LENGTH} />
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Send comment"
              accessibilityState={{ disabled: !canSend }}
              disabled={!canSend}
              onPress={() => void send()}
              style={({ pressed }) => ({
                width: 40,
                height: 40,
                borderRadius: radii.pill,
                backgroundColor: canSend ? palette.accent : palette.surfaceAlt,
                alignItems: "center",
                justifyContent: "center",
                opacity: pressed ? 0.8 : 1,
              })}
            >
              {sending ? (
                <ActivityIndicator color={palette.onAccent} />
              ) : (
                <Feather name="send" size={18} color={canSend ? palette.onAccent : palette.inkFaint} />
              )}
            </Pressable>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}
