/**
 * Touse/Bouse: the weekly campus house leaderboard (board card c175).
 *
 * Every campus-verified student casts ONE ballot a week naming a top house and,
 * optionally, a bottom house. This screen is that ballot plus the standings, and the
 * race for the term title ("Touse of Fall 26").
 *
 * TWO THINGS THIS SCREEN MUST KEEP DOING, both of which are about the fact that it
 * ranks REAL organisations publicly, bottom included (the product call is recorded on
 * c175 with the risk stated):
 *
 *   1. UNRANKED IS NOT LAST. Houses under the vote threshold render in their own quiet
 *      section with their vote count, never sorted to the bottom of the ranking. "Came
 *      last" and "three people voted" are different statements about a real chapter, and
 *      collapsing them is how a house gets called the worst on campus off noise.
 *   2. THE DENOMINATOR IS ALWAYS ON SCREEN. `ballots_cast` is captioned under the
 *      ranking, for the same reason the Secretary dashboard always states the meeting
 *      count its numbers are drawn from: a ranking with no sample size invites people
 *      to read noise as a verdict.
 *
 * DESIGN.md: header zone with eyebrow, oversized title and the gold accent bar (rule 1
 * / 10.1); exactly one gold moment, spent on the term title holder, which is the number
 * this whole feature exists to produce (rules 4 and 6); ranked rows are compact and the
 * title card breathes (rule 3, density contrast); no emojis anywhere.
 */

import { useCallback, useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import {
  castHouseBallot,
  getHouseLeaderboard,
  getHouseTitleRace,
  type HouseLeaderboard,
  type TermTitleRace,
} from "@/api/house";
import { useCampus, useSession } from "@/auth";
import {
  AppText,
  Button,
  Card,
  Chip,
  EmptyState,
  HeroCard,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { showApiError } from "@/lib/alert";
import { calendarDay } from "@/lib/dates";
import { radii, spacing, typography, useAppearance, useTheme } from "@/theme";

/** A house as the picker sees it — every chapter on campus, ranked or not. */
interface HouseOption {
  chapter_id: string;
  org_name: string;
  chapter_name: string | null;
}

function houseLabel(house: { org_name: string; chapter_name: string | null }): string {
  return house.chapter_name ? `${house.org_name} ${house.chapter_name}` : house.org_name;
}

export default function HousesScreen() {
  const palette = useTheme();
  const { campusColors } = useAppearance();
  const { user } = useSession();
  const campus = useCampus();
  const campusId = user?.campus_id ?? null;

  const [board, setBoard] = useState<HouseLeaderboard | null>(null);
  const [race, setRace] = useState<TermTitleRace | null>(null);
  const [loading, setLoading] = useState(true);

  const [voting, setVoting] = useState(false);
  const [touse, setTouse] = useState<string | null>(null);
  const [bouse, setBouse] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (campusId === null) return;
    setLoading(true);
    try {
      // Two calls, not one per house: both endpoints already aggregate server-side.
      const [leaderboard, titleRace] = await Promise.all([
        getHouseLeaderboard(campusId),
        getHouseTitleRace(campusId),
      ]);
      setBoard(leaderboard);
      setRace(titleRace);
      setTouse(leaderboard.my_ballot?.touse_chapter_id ?? null);
      setBouse(leaderboard.my_ballot?.bouse_chapter_id ?? null);
    } catch (error) {
      showApiError(error, "Couldn't load the leaderboard");
      setBoard(null);
    } finally {
      setLoading(false);
    }
  }, [campusId]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async () => {
    if (campusId === null || touse === null) return;
    setSaving(true);
    try {
      await castHouseBallot(campusId, touse, bouse);
      setVoting(false);
      await load();
    } catch (error) {
      showApiError(error, "Couldn't save your vote");
    } finally {
      setSaving(false);
    }
  };

  // Every chapter on campus, ranked or not — the ballot must be able to name a house
  // nobody has voted for yet, which is exactly the house least likely to be in `ranked`.
  const houses: HouseOption[] = [
    ...(board?.ranked ?? []).map((r) => ({
      chapter_id: r.chapter_id,
      org_name: r.org_name,
      chapter_name: r.chapter_name,
    })),
    ...(board?.unranked ?? []).map((r) => ({
      chapter_id: r.chapter_id,
      org_name: r.org_name,
      chapter_name: r.chapter_name,
    })),
  ].sort((a, b) => a.org_name.localeCompare(b.org_name));

  const hasVoted = board?.my_ballot != null;

  const picker = (
    selected: string | null,
    onPick: (id: string | null) => void,
    disabledId: string | null,
  ) => (
    <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
      {houses.map((house) => {
        const isSelected = selected === house.chapter_id;
        // A house already named on the other half of the ballot is not offerable: the
        // server rejects naming one house twice, so offering it would be a button whose
        // only outcome is an error.
        const blocked = disabledId === house.chapter_id;
        return (
          <Pressable
            key={house.chapter_id}
            accessibilityRole="button"
            accessibilityState={{ selected: isSelected, disabled: blocked }}
            disabled={blocked || saving}
            onPress={() => onPick(isSelected ? null : house.chapter_id)}
            style={({ pressed }) => ({ opacity: blocked ? 0.35 : pressed ? 0.6 : 1 })}
          >
            <Chip label={houseLabel(house)} variant={isSelected ? "accent" : "neutral"} />
          </Pressable>
        );
      })}
    </View>
  );

  return (
    <Screen
      eyebrow={campus ? campus.name.toUpperCase() : undefined}
      title="Touse"
      subtitle="One vote a week. The whole school decides."
      accentBarColor={campusColors.secondary}
    >
      {loading ? (
        <EmptyState title="Loading..." />
      ) : board === null ? (
        <EmptyState
          title="Leaderboard unavailable"
          message="We couldn't load this week's standings. Pull back in a moment."
        />
      ) : (
        <View style={{ gap: spacing.xl }}>
          {/* The term title: the one thing this whole feature exists to produce, and the
              screen's single gold moment. */}
          <HeroCard>
            <View style={{ padding: spacing.lg, gap: spacing.xs }}>
              <AppText variant="micro" tone="onAccent">
                {`TOUSE OF ${(race?.term_label ?? "").toUpperCase()}`}
              </AppText>
              {race?.leader ? (
                <>
                  <AppText
                    style={{
                      ...typography.stat,
                      fontSize: 26,
                      lineHeight: 32,
                      color: campusColors.secondary,
                    }}
                  >
                    {houseLabel(race.leader)}
                  </AppText>
                  <AppText variant="caption" tone="onAccent">
                    {`${race.leader.weekly_wins} ${
                      race.leader.weekly_wins === 1 ? "week" : "weeks"
                    } won of ${race.weeks_scored} scored`}
                  </AppText>
                </>
              ) : (
                <AppText variant="body" tone="onAccent">
                  No house has won a week yet. The title is open.
                </AppText>
              )}
            </View>
          </HeroCard>

          {/* Your ballot. */}
          <View>
            <SectionHeader
              title={hasVoted ? "Your vote this week" : "You haven't voted yet"}
              caption={
                hasVoted ? "Change it any time before the week ends" : "One ballot per student"
              }
            />
            <Card>
              {voting ? (
                <View style={{ gap: spacing.lg }}>
                  <View style={{ gap: spacing.sm }}>
                    <AppText variant="caption" tone="tertiary">
                      TOUSE
                    </AppText>
                    {picker(touse, setTouse, bouse)}
                  </View>
                  <View style={{ gap: spacing.sm }}>
                    <AppText variant="caption" tone="tertiary">
                      BOUSE (OPTIONAL)
                    </AppText>
                    {picker(bouse, setBouse, touse)}
                  </View>
                  <View style={{ flexDirection: "row", gap: spacing.sm }}>
                    <View style={{ flex: 1 }}>
                      <Button
                        label={saving ? "Saving..." : hasVoted ? "Change my vote" : "Cast my vote"}
                        onPress={() => void submit()}
                        disabled={touse === null || saving}
                      />
                    </View>
                    <Button label="Cancel" variant="ghost" onPress={() => setVoting(false)} />
                  </View>
                </View>
              ) : (
                <View style={{ gap: spacing.md }}>
                  {hasVoted ? (
                    <View style={{ gap: spacing.xs }}>
                      <AppText variant="body">
                        {`Touse: ${
                          houses.find((h) => h.chapter_id === board.my_ballot?.touse_chapter_id)
                            ? houseLabel(
                                houses.find(
                                  (h) => h.chapter_id === board.my_ballot?.touse_chapter_id,
                                )!,
                              )
                            : "a house"
                        }`}
                      </AppText>
                      <AppText variant="caption" tone="secondary">
                        {board.my_ballot?.bouse_chapter_id
                          ? `Bouse: ${
                              houses.find(
                                (h) => h.chapter_id === board.my_ballot?.bouse_chapter_id,
                              )
                                ? houseLabel(
                                    houses.find(
                                      (h) => h.chapter_id === board.my_ballot?.bouse_chapter_id,
                                    )!,
                                  )
                                : "a house"
                            }`
                          : "No Bouse named"}
                      </AppText>
                    </View>
                  ) : (
                    <AppText variant="body" tone="secondary">
                      Pick the house that ran the week. Naming a Bouse is optional.
                    </AppText>
                  )}
                  <Button
                    label={hasVoted ? "Change my vote" : "Vote"}
                    variant={hasVoted ? "secondary" : "primary"}
                    onPress={() => setVoting(true)}
                  />
                </View>
              )}
            </Card>
          </View>

          {/* This week's ranking. */}
          <View>
            <SectionHeader
              title="This week"
              caption={`${board.ballots_cast} ${
                board.ballots_cast === 1 ? "ballot" : "ballots"
              } cast · week of ${calendarDay(board.week_start).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
              })}`}
            />
            {board.ranked.length === 0 ? (
              <EmptyState
                title="Nothing ranked yet"
                message={`A house needs ${board.min_votes_to_rank} votes before it appears here. Cast yours and tell a friend.`}
              />
            ) : (
              <Card>
                {board.ranked.map((row, index) => (
                  <ListRow
                    key={row.chapter_id}
                    title={houseLabel(row)}
                    subtitle={`${row.touse_votes} Touse · ${row.bouse_votes} Bouse`}
                    left={
                      <View
                        style={{
                          width: 32,
                          height: 32,
                          borderRadius: radii.pill,
                          alignItems: "center",
                          justifyContent: "center",
                          backgroundColor: palette.surfaceAlt,
                        }}
                      >
                        <AppText style={{ ...typography.stat, color: palette.ink }}>
                          {row.rank}
                        </AppText>
                      </View>
                    }
                    right={
                      <AppText style={{ ...typography.stat, color: palette.ink }}>
                        {row.net > 0 ? `+${row.net}` : `${row.net}`}
                      </AppText>
                    }
                    divider={index < board.ranked.length - 1}
                  />
                ))}
              </Card>
            )}
          </View>

          {/* Houses that have not cleared the threshold. Deliberately its own quiet
              section rather than the tail of the ranking — see the file docstring. */}
          {board.unranked.length > 0 ? (
            <View>
              <SectionHeader
                title="Not enough votes yet"
                caption={`${board.min_votes_to_rank} votes to be ranked`}
              />
              <Card>
                {board.unranked.map((row, index) => (
                  <ListRow
                    key={row.chapter_id}
                    title={houseLabel(row)}
                    subtitle={`${row.votes} ${row.votes === 1 ? "vote" : "votes"}`}
                    divider={index < board.unranked.length - 1}
                  />
                ))}
              </Card>
            </View>
          ) : null}
        </View>
      )}
    </Screen>
  );
}
