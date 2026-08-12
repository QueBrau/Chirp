/** Yak: anonymous campus board — vote arrows + score, NO author shown anywhere (SPEC §8.3). */

import { useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import { listYaks, voteYak, type YakOut, type YakVoteValue } from "@/api/yaks";
import { AppText, Card, EmptyState, Screen } from "@/components";
import { MOCK_CAMPUS, MOCK_MY_YAK_VOTES } from "@/mocks/data";
import { spacing, useTheme } from "@/theme";

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
    <Screen title="Yak" subtitle={`${MOCK_CAMPUS.name} — anonymous`}>
      {yaks !== null && yaks.length === 0 ? (
        <EmptyState title="Quiet campus" message="Be the first to say something (anonymously)." />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(yaks ?? []).map((yak) => {
            const mine = myVotes[yak.id];
            return (
              <Card key={yak.id}>
                <View style={{ flexDirection: "row", gap: spacing.lg }}>
                  <View style={{ flex: 1, gap: spacing.sm }}>
                    <AppText>{yak.body}</AppText>
                    <AppText variant="caption" tone="tertiary">
                      {age(yak.created_at)}
                    </AppText>
                  </View>
                  <View style={{ alignItems: "center", gap: spacing.xs }}>
                    <Pressable accessibilityRole="button" onPress={() => void vote(yak, 1)}>
                      <AppText
                        variant="title"
                        style={{ color: mine === 1 ? palette.accent : palette.textTertiary }}
                      >
                        ▲
                      </AppText>
                    </Pressable>
                    <AppText style={{ fontWeight: "600" }}>{yak.score}</AppText>
                    <Pressable accessibilityRole="button" onPress={() => void vote(yak, -1)}>
                      <AppText
                        variant="title"
                        style={{ color: mine === -1 ? palette.danger : palette.textTertiary }}
                      >
                        ▼
                      </AppText>
                    </Pressable>
                  </View>
                </View>
              </Card>
            );
          })}
        </View>
      )}
    </Screen>
  );
}
