/** Native Stripe SDK surface used by the dues flow.
 *
 * Isolated behind this module because @stripe/stripe-react-native imports React
 * Native internals that Metro refuses to bundle for web — a dynamic import is not
 * enough, since Metro follows it statically. stripeSdk.web.ts is the web half.
 */

import { initPaymentSheet, initStripe, presentPaymentSheet } from "@stripe/stripe-react-native";

export interface StripeSdk {
  initStripe: typeof initStripe;
  initPaymentSheet: typeof initPaymentSheet;
  presentPaymentSheet: typeof presentPaymentSheet;
}

/** null on web (see stripeSdk.web.ts) — callers must handle its absence. */
export const stripeSdk: StripeSdk | null = {
  initStripe,
  initPaymentSheet,
  presentPaymentSheet,
};
