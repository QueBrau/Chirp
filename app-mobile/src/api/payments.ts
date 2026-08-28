/** Payments API: Stripe Connect onboarding + dues payment intents — routers/payments.py.
 *
 * Card data never touches our servers (SPEC §8.7) — this module only fetches the
 * params the Stripe SDK needs; the PaymentSheet flow lives in src/payments/dues.tsx.
 */

import { request } from "./client";

/**
 * Which rail the member is paying on. Chosen BEFORE the intent exists because
 * Stripe freezes application_fee_amount at creation, and the platform fee differs
 * per rail (1% card, 2% ACH).
 */
export type PaymentRail = "card" | "ach";

/** Response of POST /payments/connect/onboarding-link. */
export interface OnboardingLinkOut {
  url: string;
  expires_at: string;
}

/** Response of GET /chapters/{chapter_id}/payments/status. */
export interface ChapterPaymentsStatus {
  onboarded: boolean;
  charges_enabled: boolean;
  details_submitted: boolean;
}

/**
 * Where the intent behind this response already stands with Stripe (board c234).
 *
 * "awaiting_payment" is the default and is what a freshly created intent always
 * reports: the client secret is safe to open in PaymentSheet. The other two mean
 * a same-rail retry retrieved an intent Stripe has already moved past waiting on
 * the member (an ACH debit in flight, or money already taken) while our webhook
 * has not resolved the reservation yet. The client secret is still real, but
 * presenting it as a fresh checkout would ask someone to pay a second time.
 */
export type DuesIntentStatus = "awaiting_payment" | "processing" | "succeeded";

/** Response of POST /payments/dues/{cycle_id}/intent — everything PaymentSheet needs. */
export interface DuesIntentOut {
  payment_intent_client_secret: string;
  customer_session_client_secret: string;
  customer_id: string;
  publishable_key: string;
  stripe_account_id: string;
  amount_cents: number;
  application_fee_cents: number;
  rail: PaymentRail;
  payment_intent_status: DuesIntentStatus;
}

/** Platform cut per rail in basis points, mirroring stripe_service.PLATFORM_FEE_BPS. */
const PLATFORM_FEE_BPS: Record<PaymentRail, number> = { card: 100, ach: 200 };

export function platformFeeCents(amountCents: number, rail: PaymentRail): number {
  return Math.floor((amountCents * PLATFORM_FEE_BPS[rail]) / 10_000);
}

export async function createOnboardingLink(chapterId: string): Promise<OnboardingLinkOut> {
  return request<OnboardingLinkOut>("/payments/connect/onboarding-link", {
    method: "POST",
    body: { chapter_id: chapterId },
  });
}

export async function getChapterPaymentsStatus(
  chapterId: string,
): Promise<ChapterPaymentsStatus> {
  return request<ChapterPaymentsStatus>(`/chapters/${chapterId}/payments/status`);
}

export async function createDuesPaymentIntent(
  cycleId: string,
  rail: PaymentRail,
  amountCents = 0,
): Promise<DuesIntentOut> {
  return request<DuesIntentOut>(`/payments/dues/${cycleId}/intent`, {
    method: "POST",
    body: { rail },
  });
}
