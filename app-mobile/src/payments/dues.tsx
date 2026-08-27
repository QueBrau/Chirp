/** Dues payment flow: rail picker, then Stripe PaymentSheet on the chapter's account.
 *
 * Card data never touches our backend (SPEC §8.7) — the server hands back a
 * PaymentIntent client secret and the SDK takes it from there.
 *
 * The rail (card vs bank) is chosen BEFORE the intent is created: Stripe freezes
 * application_fee_amount at creation, so letting PaymentSheet pick the rail
 * afterwards would leave the platform fee mismatched with the rail actually used.
 */

import { Feather } from "@expo/vector-icons";
import { useState, type ReactElement } from "react";
import { Pressable, View } from "react-native";

import { ApiError } from "@/api/client";
import type { DuesPaymentPlanOut } from "@/api/finance";
import {
  createDuesPaymentIntent,
  platformFeeCents,
  type DuesIntentOut,
  type PaymentRail,
} from "@/api/payments";
import { AppText, Button, Card } from "@/components";
import { ProgressMeter } from "@/components/charts/ProgressMeter";
import { showAlert } from "@/lib/alert";
import { calendarDay } from "@/lib/dates";
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

/** due_date is a bare YYYY-MM-DD (a real scheduled date, not backfilled) — render
 * it as that calendar day per src/lib/dates.ts, same rule dues.tsx's cycle due
 * dates already follow. */
function installmentDueDate(isoDay: string): string {
  return calendarDay(isoDay).toLocaleDateString(undefined, { month: "long", day: "numeric" });
}

/** paid_at is a genuine timestamp written the moment a treasurer records the
 * payment (routers/finance.py's record_dues_installment_payment) — a real
 * instant, not a calendar day, so it renders directly rather than through
 * calendarDay(). */
function installmentPaidDate(isoInstant: string): string {
  return new Date(isoInstant).toLocaleDateString(undefined, { month: "long", day: "numeric" });
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

export interface PlanProgressCardProps {
  cycleName: string;
  plan: DuesPaymentPlanOut;
}

/**
 * Read-only progress view for a member's own ACTIVE installment plan (c197).
 * Replaces the pay-now / outstanding state on dues.tsx for a cycle the member
 * is on a plan for — no pay button, since installments in this build are
 * treasurer-recorded (record_dues_installment_payment), never member-initiated.
 *
 * A completed or canceled plan never reaches this component — dues.tsx only
 * renders it for status === "active", folding completed plans into the
 * existing paid/settled state and letting canceled plans fall back to the
 * normal pay-now/outstanding rendering.
 */
export function PlanProgressCard({ cycleName, plan }: PlanProgressCardProps): ReactElement {
  const palette = useTheme();
  const installments = plan.installments; // seq order, per _load_installments
  const paid = installments.filter((installment) => installment.paid_at !== null);
  const paidCents = paid.reduce((sum, installment) => sum + installment.amount_cents, 0);
  const next = installments.find((installment) => installment.paid_at === null) ?? null;

  return (
    <Card>
      <View style={{ gap: spacing.md }}>
        <View style={{ gap: spacing.xs }}>
          <AppText variant="micro" tone="secondary">
            {cycleName}
          </AppText>
          <AppText variant="headline">On a payment plan</AppText>
          <AppText variant="caption" tone="secondary">
            {`${dollars(paidCents)} of ${dollars(plan.total_cents)} paid · ${paid.length} of ${plan.installment_count} installments`}
          </AppText>
        </View>

        <ProgressMeter
          fraction={plan.installment_count > 0 ? paid.length / plan.installment_count : 0}
          label={`Installments paid: ${paid.length} of ${plan.installment_count}`}
        />

        {next !== null ? (
          <AppText variant="bodyBold">
            {`Next: ${dollars(next.amount_cents)} due ${installmentDueDate(next.due_date)}`}
          </AppText>
        ) : null}

        <View style={{ gap: spacing.sm }}>
          {installments.map((installment) => {
            const isPaid = installment.paid_at !== null;
            return (
              <View
                key={installment.id}
                style={{
                  flexDirection: "row",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: spacing.sm,
                }}
              >
                <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
                  <Feather
                    name={isPaid ? "check-circle" : "circle"}
                    size={16}
                    color={isPaid ? palette.success : palette.inkFaint}
                  />
                  <AppText variant="body" tone={isPaid ? "primary" : "secondary"}>
                    {`Installment ${installment.seq}`}
                  </AppText>
                </View>
                <AppText variant="caption" tone={isPaid ? "success" : "tertiary"}>
                  {installment.paid_at !== null
                    ? `${dollars(installment.amount_cents)} · paid ${installmentPaidDate(installment.paid_at)}`
                    : `${dollars(installment.amount_cents)} · due ${installmentDueDate(installment.due_date)}`}
                </AppText>
              </View>
            );
          })}
        </View>

        <AppText variant="caption" tone="tertiary">
          Your treasurer records each installment as it comes in — no action needed here.
        </AppText>
      </View>
    </Card>
  );
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
        showAlert("Dues paid", `${cycleName} is settled. Thanks!`);
      } else {
        showAlert(
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
      showAlert("Payment failed", message);
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
