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
import { useSession } from "@/auth";
import { currentIdentity, ownsIdentity } from "@/auth/identity";
import { showApiError } from "@/lib/alert";
import { acceptPage, beginOlderPage, collectionPage, commentOrder, mergePageRows } from "@/lib/collectionPages";
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

/** One page of thread. Matches the server's default; the server caps it at 200 (c258). */
const COMMENT_PAGE_SIZE = 50;

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
   * The thread's length, reported ONLY while this sheet holds the WHOLE thread.
   *
   * This keeps the card's chip and this sheet from ever disagreeing. Both numbers come
   * from the same server-side rule: _post_counts_select's comment_count and
   * list_comments apply the identical blocked-author filter (c109).
   *
   * c258 MADE THE OLD INVARIANT FALSE, and this is the correction rather than a
   * softening of it. list_comments used to be unpaginated, so the rows rendered here
   * WERE the count; now the first page is only the newest slice, and reporting its
   * length would tell the card that a long thread is short - a truncation dressed up
   * as a fact. So the count is reported only once `hasOlder` is false, meaning every
   * page has been loaded. Until then the card keeps the number the feed query gave it,
   * which is the authoritative count anyway.
   */
  onCountChange: (count: number) => void;
}

export function CommentsSheet({ postId, onClose, onCountChange }: CommentsSheetProps) {
  useSession(); // Re-render when the account changes, even if the post stays the same.
  const renderOwner = currentIdentity();
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [comments, setComments] = useState<PostCommentOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  /** A full page means there may be older comments behind it (c258). */
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [countKnown, setCountKnown] = useState(false);
  const queryRef = useRef({ postId, owner: renderOwner, active: true, initialLoaded: false, page: collectionPage() });
  const renderQuery = queryRef.current;
  const countCallback = useRef(onCountChange);
  countCallback.current = onCountChange;
  const currentQuery = (query: typeof queryRef.current) =>
    query === queryRef.current && query.owner === renderOwner && query.active && query.postId === postId && ownsIdentity(query.owner);
  // Same hard guard CreateSheet's submit uses: a ref is read and written
  // synchronously, so two taps landing inside one render can't both get through the
  // way a `disabled` prop one render behind would let them.
  const sendingRef = useRef(false);

  const load = async (query = renderQuery) => {
    if (!currentQuery(query)) return;
    const request = Symbol("initial comments");
    query.page.pending = request;
    query.initialLoaded = false;
    setLoadState("loading");
    setLoadingOlder(false);
    setCountKnown(false);
    try {
      const rows = await listComments(postId, { limit: COMMENT_PAGE_SIZE });
      if (!currentQuery(query) || query.page.pending !== request) return;
      const oldest = rows[0];
      acceptPage(query.page, rows.length, COMMENT_PAGE_SIZE,
        oldest ? { before: oldest.created_at, beforeId: oldest.id } : null);
      setComments(current => mergePageRows(current, rows, row => row.id, commentOrder));
      query.initialLoaded = true;
      setHasOlder(query.page.more);
      setCountKnown(true);
      setLoadState("loaded");
    } catch {
      if (!currentQuery(query) || query.page.pending !== request) return;
      setLoadState("error");
    } finally {
      if (currentQuery(query) && query.page.pending === request) query.page.pending = null;
    }
  };

  /** Fetch the page immediately before the oldest comment held, and prepend it. */
  const loadOlder = async () => {
    const query = renderQuery;
    if (!currentQuery(query)) return;
    const request = beginOlderPage(query.page), cursor = query.page.cursor;
    if (request === null || cursor === null) return;
    setLoadingOlder(true);
    try {
      const rows = await listComments(postId, {
        ...cursor,
        limit: COMMENT_PAGE_SIZE,
      });
      if (!currentQuery(query) || query.page.pending !== request) return;
      const oldest = rows[0];
      acceptPage(query.page, rows.length, COMMENT_PAGE_SIZE,
        oldest ? { before: oldest.created_at, beforeId: oldest.id } : null);
      setHasOlder(query.page.more);
      setComments(current => mergePageRows(current, rows, row => row.id, commentOrder));
    } catch (error) {
      if (!currentQuery(query) || query.page.pending !== request) return;
      showApiError(error, "Couldn't load earlier comments");
    } finally {
      if (currentQuery(query) && query.page.pending === request) {
        query.page.pending = null;
        setLoadingOlder(false);
      }
    }
  };

  useEffect(() => {
    const query = { postId, owner: renderOwner, active: true, initialLoaded: false, page: collectionPage() };
    queryRef.current.active = false;
    queryRef.current = query;
    setComments([]);
    setHasOlder(false);
    setCountKnown(false);
    setLoadingOlder(false);
    setDraft("");
    sendingRef.current = false;
    setSending(false);
    void load(query);
    return () => { query.active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [postId, renderOwner]);

  // Notify only from committed state. Updaters must stay pure, including when
  // React replays them. Sending one row never proves an initially failed thread complete.
  useEffect(() => {
    if (currentQuery(queryRef.current) && queryRef.current.initialLoaded && countKnown && !hasOlder && loadState === "loaded") {
      countCallback.current(comments.length);
    }
  }, [comments.length, countKnown, hasOlder, loadState, postId, renderOwner]);

  const canSend =
    draft.trim().length > 0 && !isOverLimit(draft, MAX_COMMENT_BODY_LENGTH) && !sending;

  const send = async () => {
    const query = renderQuery;
    if (!currentQuery(query) || !canSend || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    try {
      const created = await createComment(postId, { body: draft.trim() });
      if (!currentQuery(query)) return;
      // Appended rather than refetched: list_comments orders oldest-first, so a new
      // comment belongs exactly here, and the POST response is the same shape the
      // list returns (pinned by a backend test) including the author's name.
      setComments(current => mergePageRows([created], current, row => row.id, commentOrder));
      setDraft("");
      // Recover an initially failed page without treating the new row as a total.
      if (!query.initialLoaded) void load(query);
    } catch (error) {
      if (!currentQuery(query)) return;
      // The draft deliberately survives: same reasoning as CreateSheet's failed post,
      // where clearing the body turns "try again" into a lie.
      showApiError(error, "Couldn't post that comment");
    } finally {
      if (currentQuery(query)) {
        sendingRef.current = false;
        setSending(false);
      }
    }
  };

  const close = () => { if (!currentQuery(renderQuery)) return; renderQuery.active = false; onClose(); };
  if (!currentQuery(queryRef.current)) return null;

  return (
    <Modal transparent visible animationType="slide" onRequestClose={close}>
      {/* c131: onPress only, no accessibilityRole - see the file header. */}
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
              zIndex: 1,
            }}
          >
            <Feather name="x" size={16} color={palette.inkSecondary} />
          </Pressable>

          <AppText variant="title">
            {/* The number is claimed ONLY when the whole thread is held. With older
                pages outstanding, comments.length is the size of the page, not of the
                thread - a live check on a 60-comment thread showed this header reading
                "Comments (50)", which is the same truncation-as-fact bug that
                onCountChange guards against, one component over (c258). */}
            {loadState === "loaded" && comments.length > 0 && !hasOlder
              ? `Comments (${comments.length})`
              : "Comments"}
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
              {/* Older comments live ABOVE, so the control that fetches them sits at
                  the top of the thread where the missing rows will appear - not at the
                  bottom by the composer, which is where NEW comments go. It disappears
                  entirely once the top of the thread is loaded, rather than going
                  disabled, so it never implies there is more to read (c258). */}
              {hasOlder ? (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Load earlier comments"
                  accessibilityState={{ disabled: loadingOlder, busy: loadingOlder }}
                  disabled={loadingOlder}
                  onPress={() => void loadOlder()}
                  style={({ pressed }) => ({
                    alignSelf: "center",
                    paddingVertical: spacing.sm,
                    paddingHorizontal: spacing.lg,
                    borderRadius: radii.pill,
                    backgroundColor: palette.surfaceAlt,
                    opacity: loadingOlder ? 0.6 : pressed ? 0.8 : 1,
                  })}
                >
                  {loadingOlder ? (
                    <ActivityIndicator size="small" color={palette.inkSecondary} />
                  ) : (
                    <AppText variant="micro" tone="secondary">
                      Load earlier comments
                    </AppText>
                  )}
                </Pressable>
              ) : null}
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
