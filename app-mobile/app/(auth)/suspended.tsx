/**
 * Account-suspended wall (board c129, client side of c126).
 *
 * Reached only via the (tabs) auth guard's `status === "suspended"` redirect,
 * which fires when GET /auth/me reports a non-null suspended_at — the ONE
 * route that reaches this state at all (manager's ruling on c126: every other
 * authenticated route 403s a suspended caller before returning a body; this
 * one deliberately doesn't, specifically so the client can render this).
 *
 * DESIGN.md §10's "one gold moment, never zero" targets product surfaces —
 * Home, Yak, Orgs, the things a member uses. This is the opposite of one: a
 * screen that exists because using the product stopped being available. Gold
 * is the delight color; painting it on a suspension notice would read as
 * tone-deaf, not on-brand. The accent bar here is intentionally palette.danger
 * instead, matching what the state actually is — a considered exception to
 * the letter of the rule, not an oversight of it.
 *
 * Scope held deliberately (per the ticket): render the state well, no appeals
 * flow. The only action available is signing out.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useState } from "react";
import { View } from "react-native";

import { hasFirebaseConfig, signOutUser, useSession } from "@/auth";
import { AppText, Button, Card, Screen } from "@/components";
import { radii, spacing, useTheme } from "@/theme";

function formatSuspendedSince(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

export default function SuspendedScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { user } = useSession();
  const [signingOut, setSigningOut] = useState(false);

  const since = user?.suspended_at !== null && user?.suspended_at !== undefined
    ? formatSuspendedSince(user.suspended_at)
    : null;

  const handleSignOut = async () => {
    setSigningOut(true);
    try {
      if (hasFirebaseConfig()) {
        await signOutUser();
      }
      router.replace("/sign-in");
    } finally {
      setSigningOut(false);
    }
  };

  return (
    <Screen
      eyebrow="ACCOUNT STATUS"
      title="Your account is suspended"
      accentBarColor={palette.danger}
      scroll={false}
      showBack={false}
    >
      <View style={{ gap: spacing.lg }}>
        <View
          style={{
            width: 56,
            height: 56,
            borderRadius: radii.pill,
            backgroundColor: palette.dangerSoft,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Feather name="slash" size={26} color={palette.danger} />
        </View>

        <View style={{ gap: spacing.xs }}>
          {since !== null ? (
            <AppText variant="body" tone="secondary">
              Suspended since {since}.
            </AppText>
          ) : null}
          <AppText variant="body" tone="secondary">
            You won't be able to use Chirp while your account is suspended.
            This isn't automatic — a moderator made this decision.
          </AppText>
        </View>

        <Card>
          <AppText variant="caption" tone="tertiary">
            Think this is a mistake? Reach out to your campus admin through
            the usual channels outside the app.
          </AppText>
        </Card>

        <Button
          label={signingOut ? "Signing out…" : "Sign out"}
          onPress={() => void handleSignOut()}
          disabled={signingOut}
        />
      </View>
    </Screen>
  );
}
