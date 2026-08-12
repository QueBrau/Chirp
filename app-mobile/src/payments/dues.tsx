/** Dues payment flow (Stripe PaymentSheet) — screen-level stub until milestone 8.
 *
 * The real flow: POST /payments/dues/{cycle_id}/intent → present PaymentSheet
 * (Apple Pay / Google Pay) via the Stripe React Native SDK. Card data never
 * touches our backend (SPEC §8.7).
 */

import type { ReactElement } from "react";
import { StyleSheet, Text, View } from "react-native";

import { spacing, radii, typography, useTheme } from "@/theme";

export interface DuesPaymentScreenProps {
  cycleId: string;
  cycleName: string;
  amountCents: number;
}

/** Kick off the PaymentSheet flow. TODO(milestone-8): Stripe React Native SDK. */
export async function presentDuesPaymentSheet(cycleId: string): Promise<never> {
  throw new Error("TODO(milestone-8): Stripe PaymentSheet");
}

function formatCents(amountCents: number): string {
  return `$${(amountCents / 100).toFixed(2)}`;
}

/** Placeholder dues-payment screen — replaced by the PaymentSheet flow in milestone 8. */
export default function DuesPaymentScreen({
  cycleName,
  amountCents,
}: DuesPaymentScreenProps): ReactElement {
  const palette = useTheme();
  return (
    <View style={[styles.container, { backgroundColor: palette.bg }]}>
      <View
        style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.border }]}
      >
        <Text style={[styles.title, { color: palette.textPrimary }]}>{cycleName}</Text>
        <Text style={[styles.amount, { color: palette.accent }]}>{formatCents(amountCents)}</Text>
        <Text style={[styles.note, { color: palette.textSecondary }]}>
          In-app dues payments (Apple Pay / Google Pay) arrive in milestone 8. Until then, your
          treasurer records payments in the ledger.
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: spacing.lg,
    justifyContent: "center",
  },
  card: {
    borderWidth: 1,
    borderRadius: radii.lg,
    padding: spacing.xl,
    gap: spacing.md,
  },
  title: {
    fontSize: typography.title.fontSize,
    lineHeight: typography.title.lineHeight,
    fontWeight: typography.title.fontWeight,
  },
  amount: {
    fontSize: typography.display.fontSize,
    lineHeight: typography.display.lineHeight,
    fontWeight: typography.display.fontWeight,
  },
  note: {
    fontSize: typography.body.fontSize,
    lineHeight: typography.body.lineHeight,
    fontWeight: typography.body.fontWeight,
  },
});
