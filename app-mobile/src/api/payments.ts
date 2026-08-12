/** Payments API: Stripe Connect onboarding + dues payment intents — routers/payments.py.
 *
 * Backend routes are 501 stubs until milestone 8; card data never touches our
 * servers (SPEC §8.7) — the PaymentSheet flow lives in src/payments/dues.tsx.
 */

import { mocked, request, USE_MOCKS } from "./client";

/** Response of POST /payments/connect/onboarding-link. */
export interface OnboardingLinkOut {
  url: string;
}

/** Response of POST /payments/dues/{cycle_id}/intent — feeds Stripe PaymentSheet. */
export interface PaymentIntentOut {
  client_secret: string;
}

export async function createOnboardingLink(chapterId: string): Promise<OnboardingLinkOut> {
  if (USE_MOCKS) return mocked({ url: "https://connect.stripe.com/setup/mock-onboarding" });
  return request<OnboardingLinkOut>("/payments/connect/onboarding-link", {
    method: "POST",
    body: { chapter_id: chapterId },
  });
}

export async function createDuesPaymentIntent(cycleId: string): Promise<PaymentIntentOut> {
  if (USE_MOCKS) return mocked({ client_secret: "pi_mock_secret_not_a_real_stripe_secret" });
  return request<PaymentIntentOut>(`/payments/dues/${cycleId}/intent`, { method: "POST" });
}
