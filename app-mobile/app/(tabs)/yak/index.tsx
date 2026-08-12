/**
 * Yak: anonymous campus board (DESIGN §6/§7) — rotating yakTint card backgrounds,
 * NO mask/avatar of any kind (a small tinted dot is the only marker), VotePill
 * with active vote states.
 */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { listYaks, voteYak, type YakOut, type YakVoteValue } from "@/api/yaks";
import { AppText, Card, EmptyState, Screen, VotePill } from "@/components";
import { MOCK_CAMPUS, MOCK_MY_YAK_VOTES } from "@/mocks/data";
import { spacing, useTheme } from "@/theme";

/** Anonymity marker per DESIGN §6: 8px tinted dot, no mask/avatar of any kind. */
const DOT_SIZE = 8;

function age(iso: string): string {
  const hours = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 3_600_000));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

export default function YakScreen() {
  const palette = useTheme();
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

  return (
    <Screen title="Yak" subtitle={`${MOCK_CAMPUS.name} · anonymous`}>
      {yaks !== null && yaks.length === 0 ? (
        <EmptyState
          title="Quiet campus"
          message="Be the first to say something (anonymously)."
        />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(yaks ?? []).map((yak, index) => {
            const mine = myVotes[yak.id];
            const tint = palette.yakTints[index % palette.yakTints.length] ?? palette.surface;
            return (
              <Card key={yak.id} style={{ backgroundColor: tint }}>
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
                      <View style={{ flex: 1, backgroundColor: palette.ink, opacity: 0.28 }} />
                    </View>
                  </View>
                  <View style={{ flex: 1, gap: spacing.sm }}>
                    <AppText>{yak.body}</AppText>
                    <AppText variant="caption" tone="tertiary">
                      {age(yak.created_at)} · anonymous
                    </AppText>
                  </View>
                  <VotePill
                    score={yak.score}
                    vote={mine === 1 ? "up" : mine === -1 ? "down" : null}
                    onUpvote={() => void vote(yak, 1)}
                    onDownvote={() => void vote(yak, -1)}
                  />
                </View>
              </Card>
            );
          })}
        </View>
      )}
    </Screen>
  );
}
