/** Account type picker: greek / non_greek / alumni — one auth system, three experiences (SPEC §2.4). */

import { useRouter } from "expo-router";
import { useState } from "react";
import { View } from "react-native";

import type { AccountType } from "@/api/auth";
import { AppText, Badge, Button, Card, Screen } from "@/components";
import { spacing } from "@/theme";

const OPTIONS: { type: AccountType; title: string; description: string }[] = [
  {
    type: "greek",
    title: "Greek member",
    description: "Active brother or sister — chapter feed, messaging, dues, and the family tree.",
  },
  {
    type: "non_greek",
    title: "Non-greek",
    description: "Campus access: the anonymous board and messaging with friends.",
  },
  {
    type: "alumni",
    title: "Alumni",
    description: "Stay on the tree, mentor actives, and post to the job board.",
  },
];

export default function AccountTypeScreen() {
  const router = useRouter();
  const [selected, setSelected] = useState<AccountType>("greek");

  return (
    <Screen title="Who are you?" subtitle="Pick how you'll use Chirp — you can add a chapter later.">
      <View style={{ gap: spacing.md }}>
        {OPTIONS.map((option) => (
          <Card key={option.type} onPress={() => setSelected(option.type)}>
            <View style={{ gap: spacing.sm }}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
                <AppText variant="title" style={{ flex: 1 }}>
                  {option.title}
                </AppText>
                {selected === option.type ? <Badge label="Selected" tone="accent" /> : null}
              </View>
              <AppText variant="caption" tone="secondary">
                {option.description}
              </AppText>
            </View>
          </Card>
        ))}

        {/* TODO(milestone-1): persist account_type via POST /auth/bootstrap after Firebase sign-in. */}
        <Button
          label={selected === "greek" ? "Next: join your chapter" : "Continue"}
          style={{ marginTop: spacing.lg }}
          onPress={() => router.push("/join-chapter")}
        />
      </View>
    </Screen>
  );
}
