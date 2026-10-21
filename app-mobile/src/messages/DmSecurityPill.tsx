/**
 * The thread header's way into the safety number (board c463, E2EE milestone M5).
 *
 * A ghost pill beside Leave, the same shape as the other header-adjacent actions. It reads
 * "Safety number" until the person has marked the number in force as verified, then
 * "Verified" with a check: the small mark the design asks for. Either way it opens the safety
 * number screen. It renders only for a ready phone in a direct message: any other phone has
 * no number to compare, and this file claims nothing about encryption.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { Pressable } from "react-native";

import { AppText } from "@/components";
import { useDmTrust, useE2ee } from "@/crypto/E2eeProvider";
import { radii, spacing, typography, useTheme } from "@/theme";

export function DmSecurityPill({
  conversationId,
  peerUserId,
  peerName,
}: {
  conversationId: string;
  peerUserId: string | null;
  peerName?: string | null;
}) {
  const palette = useTheme();
  const router = useRouter();
  const { status } = useE2ee();
  const trust = useDmTrust(peerUserId);
  if (status !== "ready" || peerUserId === null) return null;
  const verified = trust.verified;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={verified ? "Verified. View safety number" : "View safety number"}
      onPress={() =>
        router.push({
          pathname: "/messages/safety-number",
          params: { peer: peerUserId, conversation: conversationId, ...(peerName ? { name: peerName } : {}) },
        })
      }
      hitSlop={spacing.sm}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.xs,
        paddingHorizontal: spacing.lg,
        paddingVertical: spacing.sm,
        borderRadius: radii.pill,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <Feather
        name={verified ? "check-circle" : "shield"}
        size={typography.caption.fontSize}
        color={verified ? palette.success : palette.inkSecondary}
      />
      <AppText variant="bodyBold" tone="secondary">
        {verified ? "Verified" : "Safety number"}
      </AppText>
    </Pressable>
  );
}
