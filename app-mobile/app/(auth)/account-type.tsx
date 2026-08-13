/**
 * Account type picker per DESIGN.md §6/§7: friendlier framing over the same
 * three backend account types (SPEC §2.4) — "I'm a student" leads since Chirp
 * serves all students, not just greek life.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useState, type ComponentProps } from "react";
import { View } from "react-native";

import type { AccountType } from "@/api/auth";
import { AppText, Button, Card, Screen } from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

type FeatherName = ComponentProps<typeof Feather>["name"];

const OPTIONS: { type: AccountType; icon: FeatherName; title: string; description: string }[] = [
  {
    type: "non_greek",
    icon: "user",
    title: "I'm a student",
    description: "Campus feed, the anonymous board, and messaging with friends.",
  },
  {
    type: "greek",
    icon: "users",
    title: "I'm in a fraternity or sorority",
    description: "Everything above, plus your chapter feed, dues, and the family tree.",
  },
  {
    type: "alumni",
    icon: "award",
    title: "I'm an alum",
    description: "Stay on the family tree, mentor actives, and post to the job board.",
  },
];

/** Selection dot: outlined ring when idle, filled accent + Feather check when picked. */
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

function OptionIcon({ name, selected }: { name: FeatherName; selected: boolean }) {
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

export default function AccountTypeScreen() {
  const router = useRouter();
  const [selected, setSelected] = useState<AccountType>("non_greek");

  return (
    <Screen title="Who are you?" subtitle="Pick how you'll use Chirp — you can add a chapter later.">
      <View style={{ gap: spacing.md }}>
        {OPTIONS.map((option) => {
          const isSelected = selected === option.type;
          return (
            <Card key={option.type} onPress={() => setSelected(option.type)}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
                <OptionIcon name={option.icon} selected={isSelected} />
                <View style={{ flex: 1, gap: spacing.xs }}>
                  <AppText variant="title">{option.title}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {option.description}
                  </AppText>
                </View>
                <SelectionMark selected={isSelected} />
              </View>
            </Card>
          );
        })}

        {/* TODO(milestone-1): persist account_type via POST /auth/bootstrap after Firebase sign-in. */}
        <Button
          label={selected === "greek" ? "Next: join your chapter" : "Continue"}
          style={{ marginTop: spacing.lg }}
          onPress={() => {
            // Finding 13: onPress used to always route to join-chapter regardless of
            // selection — only greek should land there; everyone else goes to Home.
            if (selected === "greek") {
              router.push("/join-chapter");
            } else {
              router.replace("/(tabs)/feed");
            }
          }}
        />
      </View>
    </Screen>
  );
}
