/**
 * Profile → Settings → Account type (board c381).
 *
 * (auth)/account-type.tsx sets this once, at signup, and until this screen
 * existed that was the ONLY time it could ever be set: PATCH /auth/me had no
 * account_type field, so a wrong tap there — or a tap that never registered
 * at all — mis-filed a real user permanently, with no way out anywhere in
 * the app. Profile's alumni section is keyed on this exact field
 * (profile/index.tsx), so the mistake was not cosmetic.
 *
 * Reuses ACCOUNT_TYPE_OPTIONS from src/lib/accountType.ts — the same title
 * and description as the signup screen, so "I'm an alum" means the same
 * thing here as it did there instead of drifting into a second, slightly
 * different explanation of the same three choices.
 */

import { Feather } from "@expo/vector-icons";
import { useState } from "react";
import { View } from "react-native";

import { updateProfile, type AccountType } from "@/api/auth";
import { useSession } from "@/auth";
import { AppText, Card, Screen } from "@/components";
import { ACCOUNT_TYPE_OPTIONS, type AccountTypeIconName } from "@/lib/accountType";
import { confirmAction, showApiError } from "@/lib/alert";
import { radii, spacing, typography, useTheme } from "@/theme";

/** Same visual language as (auth)/account-type.tsx's SelectionMark, kept local
 * to this screen rather than shared — only the copy (ACCOUNT_TYPE_OPTIONS) is
 * shared, matching how AppearanceScreen keeps its own swatch/row components. */
function SelectionMark({ selected }: { selected: boolean }) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: spacing.xl,
        height: spacing.xl,
        borderRadius: radii.pill,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: selected ? palette.accent : "transparent",
        borderWidth: selected ? 0 : 1,
        borderColor: palette.border,
      }}
    >
      {selected ? (
        <Feather name="check" size={typography.caption.fontSize} color={palette.onAccent} />
      ) : null}
    </View>
  );
}

function OptionIcon({ name, selected }: { name: AccountTypeIconName; selected: boolean }) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: spacing.xxl,
        height: spacing.xxl,
        borderRadius: radii.thumb,
        backgroundColor: selected ? palette.accentSoft : palette.surfaceAlt,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Feather
        name={name}
        size={typography.title.fontSize}
        color={selected ? palette.accent : palette.inkFaint}
      />
    </View>
  );
}

export default function AccountTypeSettingsScreen() {
  const { user, refresh } = useSession();
  const [savingType, setSavingType] = useState<AccountType | null>(null);

  const current = user?.account_type ?? null;

  const applyChange = async (nextType: AccountType) => {
    setSavingType(nextType);
    try {
      await updateProfile({ account_type: nextType });
      // Same reasoning as changeAvatar() above: account_type is read straight off
      // useSession()'s user in the alumni-section gate on the main Profile screen,
      // so a local setState here would leave that screen stale until a cold start.
      await refresh();
    } catch (error) {
      showApiError(error, "Couldn't change your account type");
    } finally {
      setSavingType(null);
    }
  };

  const confirmChange = (option: (typeof ACCOUNT_TYPE_OPTIONS)[number]) => {
    if (option.type === current || savingType !== null) return;
    confirmAction({
      title: `Change to "${option.title}"?`,
      message:
        option.type === "alumni"
          ? "Your profile will show an alumni info section, and you'll be able to post to job boards you belong to."
          : current === "alumni"
            ? "Your alumni info section will stop showing on your profile. It isn't deleted — it comes back if you switch back to alum."
            : undefined,
      confirmLabel: "Change",
      onConfirm: () => void applyChange(option.type),
    });
  };

  return (
    <Screen
      title="Account type"
      subtitle="Pick wrong at signup? Change it here — nothing else about your account moves."
    >
      <View style={{ gap: spacing.md }}>
        {ACCOUNT_TYPE_OPTIONS.map((option) => {
          const isSelected = option.type === current;
          const isSaving = savingType === option.type;
          return (
            <Card
              key={option.type}
              onPress={() => confirmChange(option)}
              style={savingType !== null && !isSaving ? { opacity: 0.5 } : undefined}
            >
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
                <OptionIcon name={option.icon} selected={isSelected} />
                <View style={{ flex: 1, gap: spacing.xs }}>
                  <AppText variant="title">{option.title}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {isSaving ? "Saving..." : option.description}
                  </AppText>
                </View>
                <SelectionMark selected={isSelected} />
              </View>
            </Card>
          );
        })}
      </View>
    </Screen>
  );
}
