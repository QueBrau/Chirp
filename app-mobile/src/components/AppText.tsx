/** Themed text primitive: type-scale variant + semantic palette color, zero hardcoded values. */

import { Text, type TextProps } from "react-native";

import { typography, useTheme, type Palette, type TypographyVariant } from "@/theme";

export type TextTone =
  | "primary"
  | "secondary"
  | "tertiary"
  | "accent"
  | "danger"
  | "success"
  | "onAccent";

const TONE_TO_PALETTE_KEY: Record<TextTone, keyof Palette> = {
  primary: "textPrimary",
  secondary: "textSecondary",
  tertiary: "textTertiary",
  accent: "accent",
  danger: "danger",
  success: "success",
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
