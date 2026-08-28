/**
 * Account type picker per DESIGN.md §6/§7: friendlier framing over the same
 * three backend account types (SPEC §2.4) — "I'm a student" leads since Chirp
 * serves all students, not just greek life.
 */

import { Feather } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState, type ComponentProps } from "react";
import { View } from "react-native";

import { bootstrap, type AccountType } from "@/api/auth";
import { ApiError } from "@/api/client";
import { getFirebaseAuth, hasFirebaseConfig, useSession, withInviteCode } from "@/auth";
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
  const { refresh, applyBootstrap } = useSession();
  // Invite code riding along from a deep link (join-chapter -> sign-in -> here);
  // forwarded back to join-chapter after bootstrap so the code survives onboarding.
  const { code: inviteCode } = useLocalSearchParams<{ code?: string }>();
  const [selected, setSelected] = useState<AccountType>("non_greek");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const proceed = () => {
    // Finding 13: onPress used to always route to join-chapter regardless of
    // selection — only greek should land there; everyone else goes to Home.
    if (selected === "greek") {
      router.push(withInviteCode("/join-chapter", inviteCode));
    } else {
      router.replace("/(tabs)/feed");
    }
  };

  /**
   * Persist account_type via POST /auth/bootstrap after Firebase sign-in
   * (milestone-1 TODO). No-op in demo mode (no Firebase project configured)
   * or if there's no signed-in Firebase user, so the mock flow is unchanged.
   * A 409 "already_registered" means an existing user signed back in and
   * landed here again — that's expected, not an error, so it just proceeds.
   */
  const handleContinue = async () => {
    const user = hasFirebaseConfig() ? getFirebaseAuth().currentUser : null;
    if (!user || !user.email) {
      proceed();
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const created = await bootstrap({
        email: user.email,
        display_name: user.displayName ?? user.email.split("@")[0],
        account_type: selected,
      });
      // Seed the session from the bootstrap response directly — no second
      // round trip, and navigation can't outrun a failed refresh.
      applyBootstrap(created);
      proceed();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Already registered (signed back in). Only proceed once the session
        // actually reflects it, or the guards would bounce us straight back.
        const settled = await refresh();
        if (settled) {
          proceed();
          return;
        }
        setError("We couldn't load your account. Check your connection and try again.");
        return;
      }
      setError("Couldn't finish setting up your account. Try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Screen title="Who are you?" subtitle="Pick how you'll use Chirp. You can add a chapter later.">
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

        {error !== null ? (
          <AppText variant="caption" tone="danger">
            {error}
          </AppText>
        ) : null}

        <Button
          label={
            submitting
              ? "Please wait..."
              : selected === "greek"
                ? "Next: join your chapter"
                : "Continue"
          }
          disabled={submitting}
          style={{ marginTop: spacing.lg }}
          onPress={() => void handleContinue()}
        />
      </View>
    </Screen>
  );
}
