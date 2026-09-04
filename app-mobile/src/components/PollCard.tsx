/**
 * One live poll: the question, its options as a tappable ballot, and the running
 * tally (board card c162).
 *
 * Share bars are ProgressMeter, not a new primitive and not a pie. DESIGN §11 is
 * explicit that a ratio against a whole is a meter, and each option is exactly
 * that -- its share of the votes cast. A pie per poll would make the reader
 * compare angles to learn percentages that are printed right next to them.
 *
 * RESULTS ARE VISIBLE WHILE THE POLL IS OPEN, which is what makes it a "live"
 * poll and what was asked for. The tradeoff is real and worth naming: a visible
 * running tally biases late voters toward the leader. If a chapter ever needs an
 * unbiased vote, the fix is to hide the bars until the member has voted or the
 * poll closes -- a change in this file only, since the server already returns
 * `my_option_id` for exactly that kind of gating.
 *
 * Nothing here can show who voted. The API carries no voter identity at all.
 */

import { Feather } from "@expo/vector-icons";
import { Pressable, View } from "react-native";

import type { PollOut } from "@/api/polls";
import { radii, spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";
import { Card } from "./Card";
import { Chip } from "./Chip";
import { ProgressMeter } from "./charts";

export interface PollCardProps {
  poll: PollOut;
  /** Cast or change the caller's vote. Omit to render read-only. */
  onVote?: (optionId: string) => void;
  /** Close the poll — only pass this when the caller holds polls_admin. */
  onClose?: () => void;
  /** Disables the ballot while a request is in flight. */
  busy?: boolean;
}

function shareOf(votes: number, total: number): number {
  // A poll with no votes yet is 0% everywhere, not a division by zero.
  return total === 0 ? 0 : votes / total;
}

export function PollCard({ poll, onVote, onClose, busy = false }: PollCardProps) {
  const palette = useTheme();
  const open = poll.status === "open";
  const votable = open && onVote !== undefined && !busy;

  return (
    <Card style={{ marginBottom: spacing.md }}>
      <View
        style={{
          flexDirection: "row",
          alignItems: "flex-start",
          gap: spacing.sm,
          marginBottom: spacing.xs,
        }}
      >
        <AppText variant="headline" style={{ flex: 1 }}>
          {poll.question}
        </AppText>
        <Chip label={open ? "open" : "closed"} variant={open ? "accent" : "neutral"} />
      </View>

      <AppText variant="caption" tone="secondary" style={{ marginBottom: spacing.md }}>
        {poll.total_votes === 1 ? "1 vote" : `${poll.total_votes} votes`}
        {poll.my_option_id === null && open ? " · you have not voted" : ""}
      </AppText>

      {poll.options.map((option) => {
        const mine = poll.my_option_id === option.id;
        const share = shareOf(option.votes, poll.total_votes);
        return (
          <Pressable
            key={option.id}
            onPress={votable ? () => onVote?.(option.id) : undefined}
            disabled={!votable}
            accessibilityRole="radio"
            accessibilityState={{ selected: mine, disabled: !votable }}
            // c312: the visible caption prints a bare number, but this reads aloud -
            // and it said "1 votes" on every single-vote option.
            accessibilityLabel={`${option.text}, ${
              option.votes === 1 ? "1 vote" : `${option.votes} votes`
            }, ${Math.round(share * 100)} percent`}
            style={{
              borderRadius: radii.card,
              borderWidth: 1,
              // The caller's own pick is the only row that changes color. Every
              // other row stays neutral, so "what did I choose" is answerable at
              // a glance without implying anything about anyone else.
              borderColor: mine ? palette.accent : palette.border,
              backgroundColor: mine ? palette.accentSoft : "transparent",
              padding: spacing.md,
              marginBottom: spacing.sm,
            }}
          >
            <View
              style={{
                flexDirection: "row",
                alignItems: "center",
                gap: spacing.sm,
                marginBottom: spacing.sm,
              }}
            >
              {mine ? (
                <Feather name="check-circle" size={16} color={palette.accent} />
              ) : null}
              <AppText variant={mine ? "bodyBold" : "body"} style={{ flex: 1 }}>
                {option.text}
              </AppText>
              <AppText variant="caption" tone="secondary">
                {option.votes} · {Math.round(share * 100)}%
              </AppText>
            </View>
            <ProgressMeter fraction={share} label={`${option.text} share of votes`} />
          </Pressable>
        );
      })}

      {onClose !== undefined && open ? (
        <Button
          label="Close voting"
          variant="secondary"
          onPress={onClose}
          disabled={busy}
          style={{ marginTop: spacing.xs }}
        />
      ) : null}
    </Card>
  );
}
