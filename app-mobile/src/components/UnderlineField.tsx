/**
 * UnderlineField (DESIGN §5, c385): the auth screens' text field — a `caption`
 * label above, the value row in `body` type, a 1px bottom border that turns
 * `accent` on focus, and an optional trailing icon.
 *
 * SCOPED TO AUTH ON PURPOSE. It does NOT replace `inputField()` from src/theme,
 * which is the filled surfaceAlt treatment every other form in the app uses and
 * remains the default. Two field treatments is a deliberate split — the auth
 * screen is the one place the open, line-only look is the design (braul's
 * reference shot) — and a third would mean something has gone wrong.
 *
 * The trailing slot is either a real BUTTON (the password eye toggle) or a purely
 * decorative glyph (the email at-sign), and the two are separate props rather than
 * one icon plus a nullable handler. That distinction is not cosmetic: an icon with
 * no action must not announce itself to a screen reader as a control, and the
 * shape of the props makes it impossible to ship a "button" that does nothing.
 */

import { Feather } from "@expo/vector-icons";
import type { ComponentProps } from "react";
import { useState } from "react";
import { Pressable, TextInput, View, type TextInputProps } from "react-native";

import { radii, spacing, typography, useTheme } from "@/theme";

import { AppText } from "./AppText";

type FeatherIconName = ComponentProps<typeof Feather>["name"];

/** iOS HIG / WCAG 2.5.5 minimum tappable size, same rule as c307 elsewhere. */
const TOUCH_TARGET = 44;
const TRAILING_ICON = 20;
/** Derived from the icon, never hand-picked — see c307. */
const TRAILING_HIT_SLOP = Math.ceil((TOUCH_TARGET - TRAILING_ICON) / 2);

export interface UnderlineFieldProps extends Omit<TextInputProps, "style" | "placeholderTextColor"> {
  /** Small label above the field ("E-mail", "Password"). */
  label: string;
  /** Decorative trailing glyph. Not announced, not tappable — use `action` for a control. */
  icon?: FeatherIconName;
  /** Real trailing control (the password eye toggle). Wins over `icon` when both are given. */
  action?: {
    icon: FeatherIconName;
    /** Announced to screen readers. A control with a role and no name is the c297 bug. */
    label: string;
    onPress: () => void;
  };
  /** Hint under the field ("At least 6 characters"). Never a placeholder pretending to be a value. */
  hint?: string;
}

export function UnderlineField({ label, icon, action, hint, onFocus, onBlur, ...input }: UnderlineFieldProps) {
  const palette = useTheme();
  const [focused, setFocused] = useState(false);

  return (
    <View style={{ gap: spacing.xs }}>
      <AppText variant="caption" tone="secondary">
        {label}
      </AppText>
      <View
        style={{
          flexDirection: "row",
          alignItems: "center",
          gap: spacing.sm,
          borderBottomWidth: 1,
          borderBottomColor: focused ? palette.accent : palette.border,
          paddingBottom: spacing.sm,
        }}
      >
        <TextInput
          {...input}
          onFocus={(event) => {
            setFocused(true);
            onFocus?.(event);
          }}
          onBlur={(event) => {
            setFocused(false);
            onBlur?.(event);
          }}
          placeholderTextColor={palette.inkFaint}
          style={{
            flex: 1,
            ...typography.body,
            color: palette.ink,
          }}
        />
        {action !== undefined ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={action.label}
            onPress={action.onPress}
            hitSlop={TRAILING_HIT_SLOP}
            style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1, borderRadius: radii.pill })}
          >
            <Feather name={action.icon} size={TRAILING_ICON} color={palette.inkSecondary} />
          </Pressable>
        ) : icon !== undefined ? (
          <Feather name={icon} size={TRAILING_ICON} color={palette.inkFaint} />
        ) : null}
      </View>
      {hint !== undefined ? (
        <AppText variant="caption" tone="tertiary">
          {hint}
        </AppText>
      ) : null}
    </View>
  );
}
