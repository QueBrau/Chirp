/**
 * Yak: anonymous campus board (DESIGN §6/§7, §10 rules 5/6) — the board is a
 * PLACE, not a list: it renders on a deep-navy wash of the campus primary
 * color (campusNightWash), the SAME value in both light and dark system mode,
 * so it always feels like the campus at night — instantly distinct from
 * Home's neutral canvas. Cards stay light-tinted (yakTints) floating on that
 * canvas regardless of system scheme, so all card content is pinned to the
 * LIGHT palette's ink tones on purpose (not `useTheme()`'s system-following
 * ones) — otherwise text would vanish in system dark mode. Active up-votes
 * and the single top-scoring yak's number use campus gold (§10 rule 6);
 * down-votes stay danger red. NO mask/avatar of any kind (a small tinted dot
 * is the only marker, DESIGN §6).
 */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listYaks, voteYak, type YakOut, type YakVoteValue } from "@/api/yaks";
import { AppText, EmptyState, Screen, VotePill } from "@/components";
import { MOCK_CAMPUS, MOCK_MY_YAK_VOTES } from "@/mocks/data";
import { campusNightWash, elevation, light, metrics, radii, spacing, useAppearance } from "@/theme";

/** Anonymity marker per DESIGN §6: 8px tinted dot, no mask/avatar of any kind. */
const DOT_SIZE = 8;

function age(iso: string): string {
  const hours = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 3_600_000));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

export default function YakScreen() {
  const { campusColors } = useAppearance();
  const navyWash = campusNightWash(campusColors);
  const [yaks, setYaks] = useState<YakOut[] | null>(null);
  const [myVotes, setMyVotes] = useState<Record<string, YakVoteValue>>({ ...MOCK_MY_YAK_VOTES });

  useEffect(() => {
    void listYaks(MOCK_CAMPUS.id).then(setYaks);
  }, []);

  const vote = async (yak: YakOut, value: YakVoteValue) => {
    const previous = myVotes[yak.id] ?? 0;
    if (previous === value) return; // one vote per user; PUT is idempotent
    const updated = await voteYak(yak.id, value);
    setMyVotes((votes) => ({ ...votes, [yak.id]: value }));
    setYaks((current) =>
      (current ?? []).map((y) =>
        y.id === yak.id ? { ...y, score: y.score + updated.value - previous } : y,
      ),
    );
  };

  // §10 rule 6: the single highest score on the board is the "notable number" that gets gold.
  const topScore = (yaks ?? []).reduce((max, y) => Math.max(max, y.score), -Infinity);

  return (
    <Screen backgroundColor={navyWash} scroll>
      {/* Custom header (§10.1), not Screen's `title` prop — the canvas is forced navy
          regardless of system scheme, so header text is pinned to onAccent (white)
          rather than the system-following ink/inkSecondary tones. */}
      <View style={{ marginBottom: spacing.xl, gap: spacing.xs }}>
        <AppText variant="micro" tone="onAccent">
          {MOCK_CAMPUS.name.toUpperCase()} · SPARTANS
        </AppText>
        <AppText variant="display" tone="onAccent">
          Yak
        </AppText>
        <View
          style={{
            width: metrics.accentBarWidth,
            height: metrics.accentBarHeight,
            borderRadius: metrics.accentBarRadius,
            backgroundColor: campusColors.secondary,
            marginTop: spacing.xs,
          }}
        />
        <AppText variant="caption" tone="onAccent" style={{ marginTop: spacing.xs }}>
          Anonymous and campus-wide — no names, ever.
        </AppText>
      </View>

      {yaks !== null && yaks.length === 0 ? (
        <EmptyState
          title="Quiet campus"
          message="Be the first to say something (anonymously)."
        />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(yaks ?? []).map((yak, index) => {
            const mine = myVotes[yak.id];
            const tint = light.yakTints[index % light.yakTints.length] ?? light.surface;
            const isTop = yak.score === topScore && yak.score > 0;
            return (
              <View
                key={yak.id}
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
                    <AppText style={{ color: light.ink }}>{yak.body}</AppText>
                    <AppText variant="caption" style={{ color: light.inkFaint }}>
                      {age(yak.created_at)} · anonymous
                    </AppText>
                  </View>
                  <VotePill
                    score={yak.score}
                    vote={mine === 1 ? "up" : mine === -1 ? "down" : null}
                    upColor={campusColors.secondary}
                    scoreColor={isTop ? campusColors.secondary : undefined}
                    onUpvote={() => void vote(yak, 1)}
                    onDownvote={() => void vote(yak, -1)}
                  />
                </View>
              </View>
            );
          })}
        </View>
      )}
    </Screen>
  );
}
