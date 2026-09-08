/** Payment uncertainty must never be presented as proof that nothing happened. */
import { ApiError } from "@/api/client";
import { apiErrorMessage } from "@/lib/alert";

const UNCONFIRMED = "We couldn't confirm this payment attempt. Retry with the same payment method. If it stays unresolved, contact your treasurer.";

export function duesPaymentError(error: unknown): { title: string; message: string } {
  if (!(error instanceof ApiError)) return { title: "Payment not confirmed", message: UNCONFIRMED };
  const mapped = apiErrorMessage(error);
  // The shared mapper intentionally preserves unknown codes for other screens.
  // A payment response can contain provider data: this flow never renders it.
  const message = mapped === error.detail ? UNCONFIRMED : mapped;
  const title = error.detail === "payment_reconciliation_required" ? "Payment needs review"
    : error.detail === "payment_already_in_progress" ? "Payment in progress"
    : error.detail === "payment_outcome_unconfirmed" || mapped === error.detail ? "Payment not confirmed"
    : "Payment unavailable";
  return { title, message };
}
