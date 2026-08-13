/** Dues payment flow: rail picker, then Stripe PaymentSheet on the chapter's account.
 *
 * Card data never touches our backend (SPEC §8.7) — the server hands back a
 * PaymentIntent client secret and the SDK takes it from there.
 *
 * The rail (card vs bank) is chosen BEFORE the intent is created: Stripe freezes
 * application_fee_amount at creation, so letting PaymentSheet pick the rail
 * afterwards would leave the platform fee mismatched with the rail actually used.
 */

import { useState, type ReactElement } from "react";
import { Alert, Pressable, View } from "react-native";

import { ApiError } from "@/api/client";
import {
  createDuesPaymentIntent,
  platformFeeCents,
  type DuesIntentOut,
  type PaymentRail,
} from "@/api/payments";
import { AppText, Button, Card } from "@/components";
import { radii, spacing, useTheme } from "@/theme";

import { stripeSdk } from "./stripeSdk";

export interface DuesPaymentScreenProps {
  cycleId: string;
  cycleName: string;
  amountCents: number;
  onPaid?: () => void;
}

const WEB_UNAVAILABLE = "Dues payments need the native app build.";

const RAILS: { value: PaymentRail; label: string; caption: string }[] = [
  { value: "card", label: "Card", caption: "Instant · 1% fee" },
  { value: "ach", label: "Bank account", caption: "1–3 days · 2% fee" },
];

function dollars(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

/**
 * Fetch intent params, hand them to PaymentSheet, and present it.
 *
 * Returns "paid" only for the card rail. An ACH sheet that closes successfully means
 * the debit was INITIATED — the money (and the ledger entry, which the webhook
 * writes) can be days out, so the copy must not claim dues are paid.
 */
export async function presentDuesPaymentSheet(
  cycleId: string,
  rail: PaymentRail,
  amountCents: number,
): Promise<"paid" | "initiated" | "canceled"> {
  if (stripeSdk === null) throw new Error(WEB_UNAVAILABLE);
  const { initStripe, initPaymentSheet, presentPaymentSheet } = stripeSdk;

  const params: DuesIntentOut = await createDuesPaymentIntent(cycleId, rail, amountCents);

  // stripeAccountId is what routes the charge to the CHAPTER's connected account.
  // Without it the SDK pays the platform account instead — silently, and with a
  // real card.
  await initStripe({
    publishableKey: params.publishable_key,
    stripeAccountId: params.stripe_account_id,
  });

  const init = await initPaymentSheet({
    merchantDisplayName: "Chirp",
    paymentIntentClientSecret: params.payment_intent_client_secret,
    customerSessionClientSecret: params.customer_session_client_secret,
    // ACH is a delayed payment method; the sheet refuses to show it without this.
    allowsDelayedPaymentMethods: true,
  });
  if (init.error) throw new Error(init.error.message);

  const result = await presentPaymentSheet();
  if (result.error) {
    // Not an error path — the member closed the sheet.
    if (result.error.code === "Canceled") return "canceled";
    throw new Error(result.error.message);
  }
  return rail === "card" ? "paid" : "initiated";
}

function RailOption({
  rail,
  selected,
  onSelect,
}: {
  rail: (typeof RAILS)[number];
  selected: boolean;
  onSelect: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      onPress={onSelect}
      accessibilityRole="radio"
      accessibilityState={{ selected }}
      style={{
        flex: 1,
        gap: spacing.xs,
        padding: spacing.md,
        borderRadius: radii.md,
        borderWidth: 1,
        borderColor: selected ? palette.accent : palette.border,
        backgroundColor: selected ? palette.accentSoft : palette.surface,
      }}
    >
      <AppText variant="bodyBold" tone={selected ? "accent" : "primary"}>
        {rail.label}
      </AppText>
      <AppText variant="caption" tone="secondary">
        {rail.caption}
      </AppText>
    </Pressable>
  );
}

/** Dues payment card: pick a rail, see the exact fee, then pay. */
export default function DuesPaymentScreen({
  cycleId,
  cycleName,
  amountCents,
  onPaid,
}: DuesPaymentScreenProps): ReactElement {
  const [rail, setRail] = useState<PaymentRail>("card");
  const [paying, setPaying] = useState(false);

  const feeCents = platformFeeCents(amountCents, rail);

  const pay = async () => {
    setPaying(true);
    try {
      const outcome = await presentDuesPaymentSheet(cycleId, rail, amountCents);
      if (outcome === "canceled") return;
      if (outcome === "paid") {
        Alert.alert("Dues paid", `${cycleName} is settled. Thanks!`);
      } else {
        Alert.alert(
          "Bank transfer started",
          "Bank payments take 1–3 business days to clear. Your dues will show as paid once it settles.",
        );
      }
      onPaid?.();
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.detail
          : stripeSdk === null
            ? WEB_UNAVAILABLE
            : "Payment could not be started. Try again.";
      Alert.alert("Payment failed", message);
    } finally {
      setPaying(false);
    }
  };

  return (
    <Card>
      <View style={{ gap: spacing.lg }}>
        <View style={{ gap: spacing.xs }}>
          <AppText variant="micro" tone="secondary">
            {cycleName}
          </AppText>
          <AppText variant="display">{dollars(amountCents)}</AppText>
        </View>

        <View style={{ gap: spacing.sm }}>
          <AppText variant="micro" tone="secondary">
            Pay with
          </AppText>
          <View style={{ flexDirection: "row", gap: spacing.sm }}>
            {RAILS.map((option) => (
              <RailOption
                key={option.value}
                rail={option}
                selected={rail === option.value}
                onSelect={() => setRail(option.value)}
              />
            ))}
          </View>
          <AppText variant="caption" tone="tertiary">
            Includes a {dollars(feeCents)} platform fee.
          </AppText>
        </View>

        <Button
          label={paying ? "Opening…" : `Pay ${dollars(amountCents)}`}
          onPress={pay}
          disabled={paying}
        />
      </View>
    </Card>
  );
}
