/**
 * Chirps: the anonymous campus board (DESIGN §6/§7, §10 rules 5/6) — the board is a
 * PLACE, not a list: it renders on a deep-navy wash of the campus primary
 * color (campusNightWash), the SAME value in both light and dark system mode,
 * so it always feels like the campus at night — instantly distinct from
 * Home's neutral canvas. Cards stay light-tinted (chirpTints) floating on that
 * canvas regardless of system scheme, so all card content is pinned to the
 * LIGHT palette's ink tones on purpose (not `useTheme()`'s system-following
 * ones) — otherwise text would vanish in system dark mode. Active up-votes
 * and the single top-scoring chirp's number use campus gold (§10 rule 6);
 * down-votes stay danger red. NO mask/avatar of any kind (a small tinted dot
 * is the only marker, DESIGN §6).
 *
 * The composer and the per-card overflow (report/block) control below follow
 * the same rule: they live inside light-tinted surfaces, so their text/icons
 * are pinned to the `light` palette too, never `useTheme()`.
 */

import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useState } from "react";
import { Modal, Pressable, TextInput, View } from "react-native";
import { useRouter } from "expo-router";

import { ApiError } from "@/api/client";
import { createReport, blockChirpAuthor } from "@/api/moderation";
import { createChirp, listChirps, voteChirp, type ChirpFeedOut, type ChirpVoteValue } from "@/api/chirps";
import { useCampus, useCampusAccess, useSession } from "@/auth";
import { AppText, EmptyState, Screen, VotePill } from "@/components";
import { confirmAction, showAlert } from "@/lib/alert";
import {
  campusNightWash,
  elevation,
  light,
  metrics,
  radii,
  spacing,
  typography,
  useAppearance,
} from "@/theme";

/** Anonymity marker per DESIGN §6: 8px tinted dot, no mask/avatar of any kind. */
const DOT_SIZE = 8;

function age(iso: string): string {
  const hours = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 3_600_000));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  showAlert(title, message);
}

/**
 * One choice in the action sheet below. We roll our own sheet instead of using
 * Alert for the menus because Android's Alert supports AT MOST three buttons —
 * the report-reason list (three reasons + Cancel) would have silently lost an
 * option there. Alert is still fine for the confirm/error dialogs, which never
 * exceed three.
 */
interface SheetOption {
  label: string;
  destructive?: boolean;
  onPress: () => void;
}

export default function ChirpScreen() {
  const router = useRouter();
  const { campusColors } = useAppearance();
  const navyWash = campusNightWash(campusColors);

  // The (tabs) layout gates on useSession().status === "ready" before this screen
  // can ever mount, so `user` is guaranteed populated here — no separate /auth/me
  // fetch needed (that would just duplicate SessionProvider's own call).
  const { user } = useSession();
  const campusId = user?.campus_id ?? null;
  // c110: since c88 the board is gated on a verified .edu, not on having a
  // campus. A chapter-joined user has campusId set and is still refused, so
  // loading on campusId alone fires a request that comes back 403.
  const access = useCampusAccess();

  const campus = useCampus();
  const [chirps, setChirps] = useState<ChirpFeedOut[] | null>(null);
  const [myVotes, setMyVotes] = useState<Record<string, ChirpVoteValue>>({});
  const [composerText, setComposerText] = useState("");
  const [posting, setPosting] = useState(false);
  const [sheet, setSheet] = useState<{ title: string; options: SheetOption[] } | null>(null);

  const loadChirps = useCallback(async (id: string) => {
    const feed = await listChirps(id);
    setChirps(feed);
    // Server-owned vote state: seed from each item's own `my_vote`.
    const votes: Record<string, ChirpVoteValue> = {};
    for (const chirp of feed) {
      if (chirp.my_vote === 1 || chirp.my_vote === -1) votes[chirp.id] = chirp.my_vote;
    }
    setMyVotes(votes);
  }, []);

  useEffect(() => {
    if (campusId === null || access !== "ok") return; // no campus, or not verified yet
    void loadChirps(campusId).catch((error: unknown) => showApiError(error, "Couldn't load the board"));
  }, [campusId, access, loadChirps]);


  const vote = async (chirp: ChirpFeedOut, value: ChirpVoteValue) => {
    const previous: number = myVotes[chirp.id] ?? 0;
    if (previous === value) return; // one vote per user; PUT is idempotent

    // Optimistic score adjustment, rolled back below if the request fails.
    setMyVotes((votes) => ({ ...votes, [chirp.id]: value }));
    setChirps((current) =>
      (current ?? []).map((y) =>
        y.id === chirp.id ? { ...y, score: y.score + value - previous } : y,
      ),
    );

    try {
      await voteChirp(chirp.id, value);
    } catch (error) {
      setMyVotes((votes) => {
        const next = { ...votes };
        if (previous === 1 || previous === -1) next[chirp.id] = previous;
        else delete next[chirp.id];
        return next;
      });
      setChirps((current) =>
        (current ?? []).map((y) =>
          y.id === chirp.id ? { ...y, score: y.score - (value - previous) } : y,
        ),
      );
      showApiError(error, "Couldn't save your vote");
    }
  };

  const submitChirp = async () => {
    const body = composerText.trim();
    if (body.length === 0 || posting || campusId === null) return;
    setPosting(true);
    try {
      const created = await createChirp(campusId, { body });
      setChirps((current) => [{ ...created, my_vote: null }, ...(current ?? [])]);
      setComposerText("");
    } catch (error) {
      showApiError(error, "Couldn't post that");
    } finally {
      setPosting(false);
    }
  };

  const submitReport = async (chirp: ChirpFeedOut, reason: string) => {
    try {
      await createReport({ target_type: "chirp", target_id: chirp.id, reason });
      showAlert("Reported", "Thanks for letting us know.");
    } catch (error) {
      showApiError(error, "Couldn't send that report");
    }
  };

  const submitBlock = async (chirp: ChirpFeedOut) => {
    try {
      await blockChirpAuthor(chirp.id);
      // Drop the tapped card immediately so the UI reacts at once...
      setChirps((current) => (current ?? []).filter((y) => y.id !== chirp.id));
      // ...then refetch, because the server hides EVERY chirp by that author, not
      // just this one. Without this the author's other posts linger on screen and
      // contradict the promise the confirmation just made ("posts", plural).
      if (campusId !== null) await loadChirps(campusId);
    } catch (error) {
      showApiError(error, "Couldn't block that");
    }
  };

  const openReportReasons = (chirp: ChirpFeedOut) => {
    setSheet({
      title: "What's wrong with it?",
      options: ["Spam", "Harassment", "Inappropriate"].map((reason) => ({
        label: reason,
        onPress: () => void submitReport(chirp, reason),
      })),
    });
  };

  const confirmBlock = (chirp: ChirpFeedOut) => {
    // The client genuinely doesn't know who posted this (SPEC §8.3) — the copy
    // must never imply otherwise.
    confirmAction({
      title: "Block?",
      message: "You won't see posts from this person again.",
      confirmLabel: "Block",
      destructive: true,
      order: "confirm-first",
      onConfirm: () => void submitBlock(chirp),
    });
  };

  const openMenu = (chirp: ChirpFeedOut) => {
    setSheet({
      title: "More options",
      options: [
        { label: "Report", onPress: () => openReportReasons(chirp) },
        { label: "Block", destructive: true, onPress: () => confirmBlock(chirp) },
      ],
    });
  };

  // §10 rule 6: the single highest score on the board is the "notable number" that gets gold.
  const topScore = (chirps ?? []).reduce((max, y) => Math.max(max, y.score), -Infinity);
  const canCompose = composerText.trim().length > 0 && !posting;

  return (
    <Screen backgroundColor={navyWash} scroll>
      {/* Custom header (§10.1), not Screen's `title` prop — the canvas is forced navy
          regardless of system scheme, so header text is pinned to onAccent (white)
          rather than the system-following ink/inkSecondary tones. */}
      <View style={{ marginBottom: spacing.xl, gap: spacing.xs }}>
        {/* Real campus name via GET /campuses/{id} (c46). Rendered only once it
            resolves — an absent eyebrow beats a wrong one. The old value was
            MOCK_CAMPUS plus a hardcoded "· SPARTANS", which is UNCG's mascot:
            wrong for every other campus, and CampusOut has no mascot field to
            replace it with. */}
        {campus !== null ? (
          <AppText variant="micro" tone="onAccent">
            {campus.name.toUpperCase()}
          </AppText>
        ) : null}
        {/* Hand-rolled rather than Screen's, because this canvas is forced navy and the
            header text is pinned to onAccent. Kept structurally identical to Screen's so
            the two headers cannot drift: bar LEADS the title, no alignItems so it
            inherits `stretch` and matches the title's height. */}
        <View style={{ flexDirection: "row", gap: spacing.sm }}>
          <View
            style={{
              width: metrics.accentBarWidth,
              alignSelf: "stretch",
              borderRadius: metrics.accentBarRadius,
              backgroundColor: campusColors.secondary,
            }}
          />
          <AppText variant="display" tone="onAccent">
            Chirps
          </AppText>
        </View>
        <AppText variant="caption" tone="onAccent" style={{ marginTop: spacing.xs }}>
          Anonymous and campus-wide — no names, ever.
        </AppText>
      </View>

      {/* c175: the house leaderboard is the other campus-wide surface and shares Chirps'
          electorate (verified .edu), so it hangs off this tab rather than earning a
          sixth one. Deliberately a quiet row, not a hero: Chirps is the PLACE this screen
          is (DESIGN.md rule 5) and a second loud card here would fight it. */}
      {access === "ok" && campusId !== null ? (
        <Pressable
          accessibilityRole="button"
          onPress={() => router.push("/(tabs)/chirp/houses")}
          style={({ pressed }) => ({
            opacity: pressed ? 0.7 : 1,
            marginBottom: spacing.lg,
            paddingVertical: spacing.md,
            paddingHorizontal: spacing.lg,
            borderRadius: radii.card,
            // The canvas here is forced navy in BOTH schemes, so this cannot use a
            // system-following surface token - it would go near-black in dark mode on an
            // already-dark wash. A translucent white sits on the navy in either scheme.
            backgroundColor: "rgba(255,255,255,0.10)",
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "space-between",
          })}
        >
          <View style={{ gap: 2 }}>
            <AppText variant="headline" tone="onAccent">
              Touse of the week
            </AppText>
            <AppText variant="caption" tone="onAccent">
              One vote a week. The whole school decides.
            </AppText>
          </View>
          <Feather name="chevron-right" size={20} color={campusColors.secondary} />
        </Pressable>
      ) : null}

      {access === "loading" ? (
        <EmptyState title="Opening the board..." />
      ) : access === "unverified" || access === "lapsed" ? (
        // The designed refusal (c110). Chirp is the surface the .edu rule exists
        // for — a stranger reading your school's anonymous board is precisely
        // what it costs something to allow.
        <EmptyState
          title={access === "lapsed" ? "Confirm your school again" : "Students only"}
          message={
            access === "lapsed"
              ? "It's been a year since you last confirmed your .edu address. Verify again to get back on the board."
              : "Chirp is your school's anonymous board, so it's limited to students. Confirm your .edu address to join in."
          }
          actionLabel={access === "lapsed" ? "Verify again" : "Verify my .edu"}
          onAction={() => router.push("/(auth)/verify-campus")}
        />
      ) : campusId === null ? (
        // c96: same dead end as the campus feed, same fix. Joining an org
        // grants a campus (derived from the chapter, never client-asserted),
        // so there is now an actual next step to point at.
        <EmptyState
          title="Campus-only"
          message="Chirp needs a campus. Join your org from the Orgs tab and it opens up."
        />
      ) : (
        <>
          {/* Composer: light-tinted surface like the cards below, visually subordinate
              to them (no display-scale text, no gold). */}
          <View
            style={{
              backgroundColor: light.surface,
              borderRadius: radii.card,
              borderWidth: 1,
              borderColor: light.border,
              padding: spacing.md,
              marginBottom: spacing.md,
              gap: spacing.sm,
              ...elevation.card,
            }}
          >
            <TextInput
              value={composerText}
              onChangeText={setComposerText}
              placeholder="Say something anonymously…"
              placeholderTextColor={light.inkFaint}
              multiline
              style={{
                minHeight: 20,
                fontSize: typography.body.fontSize,
                lineHeight: typography.body.lineHeight,
                color: light.ink,
              }}
            />
            <View style={{ flexDirection: "row", justifyContent: "flex-end" }}>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Post"
                accessibilityState={{ disabled: !canCompose }}
                disabled={!canCompose}
                onPress={() => void submitChirp()}
                style={({ pressed }) => ({
                  width: 36,
                  height: 36,
                  borderRadius: radii.pill,
                  alignItems: "center",
                  justifyContent: "center",
                  backgroundColor: light.accent,
                  opacity: !canCompose ? 0.4 : pressed ? 0.8 : 1,
                })}
              >
                <Feather name="send" size={16} color={light.onAccent} />
              </Pressable>
            </View>
          </View>

          {chirps !== null && chirps.length === 0 ? (
            <EmptyState
              title="Quiet campus"
              message="Be the first to say something (anonymously)."
            />
          ) : (
            <View style={{ gap: spacing.md }}>
              {(chirps ?? []).map((chirp, index) => {
                const mine = myVotes[chirp.id];
                const tint = light.chirpTints[index % light.chirpTints.length] ?? light.surface;
                const isTop = chirp.score === topScore && chirp.score > 0;
                return (
                  <View
                    key={chirp.id}
                    style={{
                      backgroundColor: tint,
                      borderRadius: radii.card,
                      borderWidth: 1,
                      borderColor: light.border,
                      padding: isTop ? spacing.xl : spacing.lg,
                      ...elevation.card,
                    }}
                  >
                    <View style={{ flexDirection: "row", gap: spacing.md }}>
                      {/* No identity, ever (SPEC §8.3) — a small tinted dot is the only marker. */}
                      <View style={{ paddingTop: spacing.xs }}>
                        <View
                          style={{
                            width: DOT_SIZE,
                            height: DOT_SIZE,
                            borderRadius: DOT_SIZE / 2,
                            overflow: "hidden",
                            backgroundColor: tint,
                          }}
                        >
                          <View style={{ flex: 1, backgroundColor: light.ink, opacity: 0.28 }} />
                        </View>
                      </View>
                      <View style={{ flex: 1, gap: spacing.sm }}>
                        <AppText style={{ color: light.ink }}>{chirp.body}</AppText>
                        <View
                          style={{
                            flexDirection: "row",
                            alignItems: "center",
                            justifyContent: "space-between",
                          }}
                        >
                          <AppText variant="caption" style={{ color: light.inkFaint }}>
                            {age(chirp.created_at)} · anonymous
                          </AppText>
                          {/* Overflow control (report/block) — NOT an avatar, NOT a mask;
                              as discreet as the timestamp it sits next to. */}
                          <Pressable
                            accessibilityRole="button"
                            accessibilityLabel="More options"
                            hitSlop={spacing.sm}
                            onPress={() => openMenu(chirp)}
                          >
                            <Feather name="more-horizontal" size={14} color={light.inkFaint} />
                          </Pressable>
                        </View>
                      </View>
                      <VotePill
                        score={chirp.score}
                        vote={mine === 1 ? "up" : mine === -1 ? "down" : null}
                        upColor={campusColors.secondary}
                        scoreColor={isTop ? campusColors.secondary : undefined}
                        onUpvote={() => void vote(chirp, 1)}
                        onDownvote={() => void vote(chirp, -1)}
                      />
                    </View>
                  </View>
                );
              })}
            </View>
          )}
        </>
      )}

      {/* Action sheet for the overflow menu and the report-reason list. Card content
          is pinned to the `light` palette like everything else on this board. */}
      <Modal
        transparent
        visible={sheet !== null}
        animationType="fade"
        onRequestClose={() => setSheet(null)}
      >
        <Pressable
          onPress={() => setSheet(null)}
          style={{
            flex: 1,
            justifyContent: "flex-end",
            backgroundColor: "rgba(16,18,35,0.45)",
            padding: spacing.md,
          }}
        >
          <View
            style={{
              backgroundColor: light.surface,
              borderRadius: radii.card,
              overflow: "hidden",
            }}
          >
            <AppText
              variant="micro"
              style={{ color: light.inkFaint, padding: spacing.md, paddingBottom: spacing.sm }}
            >
              {sheet?.title.toUpperCase()}
            </AppText>
            {(sheet?.options ?? []).map((option) => (
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
                  borderTopColor: light.border,
                  backgroundColor: pressed ? light.border : "transparent",
                })}
              >
                <AppText style={{ color: option.destructive ? light.danger : light.ink }}>
                  {option.label}
                </AppText>
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
              backgroundColor: light.surface,
              borderRadius: radii.card,
              opacity: pressed ? 0.8 : 1,
            })}
          >
            <AppText style={{ color: light.ink }}>Cancel</AppText>
          </Pressable>
        </Pressable>
      </Modal>
    </Screen>
  );
}
