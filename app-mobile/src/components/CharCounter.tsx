/**
 * Remaining-characters counter for a composer (board card c251).
 *
 * The backend caps every content body (c245) and 422s anything longer. Before this,
 * a student who pasted something long found that out only after hitting send, with
 * their words already written and nothing on screen having warned them.
 *
 * THREE DELIBERATE CHOICES, all of them about not being a nag:
 *
 * 1. It is INVISIBLE until you are inside the last tenth of the allowance. A counter
 *    pinned there from the first character turns a chirp into a homework assignment;
 *    one that appears as you approach the limit reads as the warning it is. Nothing
 *    reflows when it arrives - the composer reserves no space for it, because it
 *    renders beside controls that are already laid out.
 * 2. It counts DOWN, not up. "180 left" is the number the writer needs; "1820/2000"
 *    is arithmetic homework at the moment they are least interested in doing it.
 * 3. It NEVER truncates and the text is never blocked. Going over is allowed, shown
 *    in danger tone with a negative count, and only the send control refuses. Losing
 *    someone's sentence to a silent truncation is worse than the 422 this replaces.
 *
 * Tabular numerals on purpose: the count changes on every keystroke, and proportional
 * digits make it jitter sideways while you type. Same reasoning DESIGN.md gives for
 * money and scores.
 */

import { AppText } from "./AppText";
import { charsRemaining, shouldShowCounter } from "@/lib/contentLimits";

export interface CharCounterProps {
  /** The raw composer value. Trimmed internally, because that is what the server sees. */
  value: string;
  /** The matching MAX_*_LENGTH from @/lib/contentLimits — never a literal. */
  limit: number;
}

export function CharCounter({ value, limit }: CharCounterProps) {
  if (!shouldShowCounter(value, limit)) return null;

  const remaining = charsRemaining(value, limit);
  const over = remaining < 0;

  return (
    <AppText
      variant="micro"
      tone={over ? "danger" : "warning"}
      // Announced as words rather than as a bare number, which on its own gives a
      // screen-reader user no idea what it counts.
      accessibilityLabel={
        over
          ? `${Math.abs(remaining)} characters over the limit`
          : `${remaining} characters left`
      }
      style={{ fontVariant: ["tabular-nums"] }}
    >
      {remaining}
    </AppText>
  );
}
