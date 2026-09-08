/**
 * Pill button per DESIGN.md §5 — 52 tall. Variants:
 * primary (accent bg / white), secondary (accentSoft / accent),
 * ghost (transparent / inkSecondary), destructive (dangerSoft / danger),
 * brand (accent bg / campus SECONDARY label), neutral (surfaceAlt / ink).
 * "danger" is a legacy alias for destructive.
 *
 * `neutral` (c385) is the quiet filled button: a real surface, no accent in it at
 * all. It exists because `secondary` USED TO BE unreadable in dark mode whenever
 * the accent was dark - the default accent source is campus primary, and UNCG's
 * navy label on the accentSoft fill measured 1.18:1 there (10.96:1 in light, so
 * the defect was dark-only). surfaceAlt + ink is ~15:1 in both modes by
 * construction. Reach for it when a filled button should not be an accent moment;
 * the sign-in screen's Apple/Google row is the first user. `secondary` itself is
 * now fixed everywhere it appears (board c386): its label runs through
 * `secondaryLabelColor()` (theme/colorUtils.ts), which lifts a too-dark accent
 * just enough to clear 4.5:1 against its own translucent fill in dark mode, and
 * returns the accent unchanged in light mode by construction.
 *
 * `brand` (c385) is the gold-moment CTA — §10.4 rule 4, one per screen. It is a
 * NAMED VARIANT rather than a `labelColor` prop on purpose: an arbitrary-colour
 * escape hatch on the one shared button is how a design system stops being one.
 * It reads campus secondary from useAppearance() itself, so there is no colour to
 * pass and none to get wrong. Solid accent + campus secondary is the same pairing
 * the floating tab bar already ships (§5, c310) and is UNCG's own; gold on navy
 * measures ~8.6:1 in both modes.
 */

import { Pressable, type ViewStyle } from "react-native";

import { metrics, radii, secondaryLabelColor, spacing, useAppearance, useTheme } from "@/theme";

import { AppText, type TextTone } from "./AppText";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "destructive"
  | "danger"
  | "brand"
  | "neutral";

export interface ButtonProps {
  label: string;
  onPress?: () => void;
  variant?: ButtonVariant;
  disabled?: boolean;
  style?: ViewStyle;
}

export function Button({ label, onPress, variant = "primary", disabled = false, style }: ButtonProps) {
  const palette = useTheme();
  const { campusColors } = useAppearance();

  const container: ViewStyle = {
    minHeight: metrics.buttonHeight,
    borderRadius: radii.pill,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.xl,
    alignItems: "center",
    justifyContent: "center",
  };
  let tone: TextTone = "onAccent";
  /** Set by `brand` and `secondary` (c386) — every other variant names its colour with a tone token alone. */
  let labelColor: string | null = null;

  switch (variant) {
    case "primary":
      container.backgroundColor = palette.accent;
      tone = "onAccent";
      break;
    case "secondary":
      container.backgroundColor = palette.accentSoft;
      tone = "accent";
      labelColor = secondaryLabelColor(palette.accent, palette.bg, palette.mode, palette.ink);
      break;
    case "ghost":
      container.backgroundColor = "transparent";
      tone = "secondary";
      break;
    case "destructive":
    case "danger":
      container.backgroundColor = palette.dangerSoft;
      tone = "danger";
      break;
    case "brand":
      container.backgroundColor = palette.accent;
      labelColor = campusColors.secondary;
      break;
    case "neutral":
      container.backgroundColor = palette.surfaceAlt;
      tone = "primary";
      break;
  }

  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [container, { opacity: disabled ? 0.5 : pressed ? 0.8 : 1 }, style]}
    >
      <AppText variant="bodyBold" tone={tone} style={labelColor === null ? undefined : { color: labelColor }}>
        {label}
      </AppText>
    </Pressable>
  );
}
