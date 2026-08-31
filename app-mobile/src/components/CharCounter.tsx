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
 *    reflows when it arrives - it renders beside controls that are already laid out.
 * 2. It counts DOWN, not up. "180 left" is the number the writer needs; "1820/2000"
 *    is arithmetic homework at the moment they are least interested in doing it.
 * 3. It NEVER truncates and the text is never blocked. Going over is allowed, shown
 *    as a negative count, and only the send control refuses. Losing someone's
 *    sentence to a silent truncation is worse than the 422 this replaces.
 *
 * WHY A FILLED PILL RATHER THAN COLOURED TEXT, which is what this was first built as
 * and what the live QA pass rejected. `warning` as a text colour is #F5A623, and on
 * the white composer card that is 2.03:1 - below even the 3:1 threshold for UI
 * elements, let alone the 4.5:1 that 11px micro type needs. It LOOKED fine in a
 * screenshot and would have been unreadable on a phone outdoors, which is the exact
 * failure mode "token-correct" hides. Routing it through Chip's warningSoft tint is
 * worse, not better: amber on #FDF3E1 measures 1.84:1.
 *
 * Inverting it fixes the contrast without inventing a colour: the semantic token
 * becomes the FILL and the text is near-black on top. Measured, both modes:
 *   warning fill  9.15:1 light / 10.78:1 dark
 *   danger  fill  4.74:1 light /  6.39:1 dark
 * all above 4.5:1. `light.ink` is the text in both modes because both palettes'
 * warning and danger are light-chroma fills - the same reason this file's callers
 * already pin `light.*` for fixed-on-light surfaces.
 *
 * Tabular numerals on purpose: the count changes on every keystroke, and
 * proportional digits make it jitter sideways while you type. Same reasoning
 * DESIGN.md gives for money and scores.
 */

import { View } from "react-native";

import { light, radii, spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { charsRemaining, shouldShowCounter } from "@/lib/contentLimits";

export interface CharCounterProps {
  /** The raw composer value. Trimmed internally, because that is what the server sees. */
  value: string;
  /** The matching MAX_*_LENGTH from @/lib/contentLimits — never a literal. */
  limit: number;
}

export function CharCounter({ value, limit }: CharCounterProps) {
  const palette = useTheme();
  if (!shouldShowCounter(value, limit)) return null;

  const remaining = charsRemaining(value, limit);
  const over = remaining < 0;

  return (
    <View
      style={{
        backgroundColor: over ? palette.danger : palette.warning,
        borderRadius: radii.pill,
        paddingHorizontal: spacing.sm,
        paddingVertical: 2,
      }}
    >
      <AppText
        variant="micro"
        // Announced as words rather than as a bare number, which on its own gives a
        // screen-reader user no idea what it counts.
        accessibilityLabel={
          over
            ? `${Math.abs(remaining)} characters over the limit`
            : `${remaining} characters left`
        }
        style={{ color: light.ink, fontVariant: ["tabular-nums"] }}
      >
        {remaining}
      </AppText>
    </View>
  );
}
