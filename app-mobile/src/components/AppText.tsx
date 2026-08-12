/** Themed text primitive: type-scale variant + semantic palette color, zero hardcoded values. */

import { Text, type TextProps } from "react-native";

import { typography, useTheme, type Palette, type TypographyVariant } from "@/theme";

export type TextTone =
  | "primary"
  | "secondary"
  | "tertiary"
  | "faint"
  | "accent"
  | "danger"
  | "success"
  | "warning"
  | "onAccent";

const TONE_TO_PALETTE_KEY: Record<TextTone, keyof Pick<
  Palette,
  "ink" | "inkSecondary" | "inkFaint" | "accent" | "danger" | "success" | "warning" | "onAccent"
>> = {
  primary: "ink",
  secondary: "inkSecondary",
  tertiary: "inkFaint",
  faint: "inkFaint",
  accent: "accent",
  danger: "danger",
  success: "success",
  warning: "warning",
  onAccent: "onAccent",
};

export interface AppTextProps extends TextProps {
  variant?: TypographyVariant;
  tone?: TextTone;
}

export function AppText({ variant = "body", tone = "primary", style, ...rest }: AppTextProps) {
  const palette = useTheme();
  return (
    <Text
      {...rest}
      style={[typography[variant], { color: palette[TONE_TO_PALETTE_KEY[tone]] }, style]}
    />
  );
}
