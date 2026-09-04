/**
 * Alert/confirm helper (board c183). react-native-web does not implement
 * Alert AT ALL — Alert.alert is a silent no-op there: no dialog ever renders,
 * no button is ever pressed, and any onPress callback (including the
 * destructive one on a confirm dialog) simply never fires. There is no
 * error, no warning, nothing in the console — the tap just does nothing.
 *
 * This was shipped and live before anyone noticed: the moderation screen's
 * Remove/Dismiss buttons called Alert.alert to confirm, and on web that
 * confirm dialog never appeared, so doRemove/doDismiss were never reached.
 * Every other confirm/error Alert.alert call in the app had the exact same
 * bug, just less visible than a stuck moderation queue.
 *
 * confirmAction() and showAlert() are the ONLY sanctioned way to alert or
 * confirm in this app from here on. Every NEW confirm or error alert must
 * use one of these two — a bare `Alert.alert(...)` call will silently
 * re-break on web exactly like c183 did, with nothing to catch it short of
 * a live web QA pass.
 */

import { Alert, Platform, type AlertButton } from "react-native";

import { ApiError } from "@/api/client";

export interface ConfirmActionOptions {
  title: string;
  /** Optional, matching Alert.alert's own signature — some call sites (e.g.
   * president.tsx's role-change confirms) have no secondary detail line. */
  message?: string;
  confirmLabel: string;
  /** Defaults to "Cancel" — override for call sites that used a different
   * label ("Keep it", "Keep", ...). */
  cancelLabel?: string;
  /** Styles the confirm button as destructive (red) on native. */
  destructive?: boolean;
  /**
   * Native button order. iOS/Android render Alert.alert's buttons array in
   * the order given, so this exists purely to reproduce each call site's
   * PRE-EXISTING order exactly rather than silently reordering it during the
   * migration. "cancel-first" (default) matches most of this codebase's
   * confirm dialogs (Cancel, then the confirm/destructive action);
   * "confirm-first" matches the handful that put the confirm/destructive
   * button first (moderation.tsx, chirps/index.tsx, MediaPostCard.tsx).
   */
  order?: "cancel-first" | "confirm-first";
  onConfirm: () => void;
}

/**
 * Confirm dialog with Cancel + one action button, e.g. "Remove this chirp?".
 * Web has no equivalent of a styled two-button native alert, so this falls
 * back to `window.confirm` (title + message concatenated, one OK/Cancel
 * pair) — OK maps to onConfirm, Cancel does nothing, same as native Cancel.
 */
export function confirmAction({
  title,
  message,
  confirmLabel,
  cancelLabel = "Cancel",
  destructive = false,
  order = "cancel-first",
  onConfirm,
}: ConfirmActionOptions): void {
  if (Platform.OS === "web") {
    const text = message ? `${title}\n\n${message}` : title;
    if (window.confirm(text)) onConfirm();
    return;
  }

  const confirmButton: AlertButton = {
    text: confirmLabel,
    style: destructive ? "destructive" : "default",
    onPress: onConfirm,
  };
  const cancelButton: AlertButton = { text: cancelLabel, style: "cancel" };

  Alert.alert(
    title,
    message,
    order === "cancel-first" ? [cancelButton, confirmButton] : [confirmButton, cancelButton],
  );
}

/**
 * Single-button informational/error alert, e.g. "Couldn't load reports".
 * Web falls back to `window.alert` — no callback to preserve, since plain
 * Alert.alert(title, message) has none either.
 */
export function showAlert(title: string, message?: string): void {
  if (Platform.OS === "web") {
    window.alert(message ? `${title}\n\n${message}` : title);
    return;
  }
  Alert.alert(title, message);
}

/**
 * Server `detail` codes mapped to copy a person can act on (c300).
 *
 * WHY THIS EXISTS. `apiErrorMessage` below returned `error.detail` verbatim, and
 * every detail this backend raises is a snake_case machine code —
 * `forbidden("campus_unverified")`, `not_found("chapter_not_found")`. So the alert
 * body an unverified member read after picking the campus audience was literally
 * the string "campus_unverified".
 *
 * Mapped HERE, not in the screen that reported it. These codes are not one screen's
 * problem: `require_verified_campus` raises both from core/campus_access.py, and
 * events.py raises them again independently, so the same raw string also reaches the
 * user through the RSVP and event-detail paths. A special-case inside CreateSheet's
 * own catch would have fixed the one screen that happened to get reported and left
 * the identical string raw everywhere else.
 *
 * THE TWO CODES GET DIFFERENT COPY ON PURPOSE, and one of them deliberately does not
 * mention verifying. `require_verified_campus` checks campus membership FIRST, so
 * `not_your_campus` means the content lives on a campus that is not theirs — no
 * amount of .edu verification changes that, and a "verify your .edu" line would hand
 * that user an action that cannot possibly work. The server guards the same trap for
 * the same reason at events.py:141 ("hints that verifying would help. It would not").
 *
 * SCOPE, stated honestly and RE-DERIVED (c315 batch 1). 34 codes are mapped below;
 * 78 codes the backend raises still fall through verbatim, across the 58 client call
 * sites that render them through this function.
 *
 * THE NUMBERS HERE USED TO BE WRONG, which is worth recording rather than quietly
 * fixing. c300 said "69 raised, 67 remaining". The real count of distinct codes this
 * backend raises is 112: that census was built by grepping the `forbidden()` /
 * `not_found()` / `conflict()` / `bad_request()` / `unauthorized()` helpers, and it
 * never checked whether services and middleware used them. They do not — campus
 * verification, storage, email and the auth middleware raise
 * `HTTPException(detail="...")` directly, 43 codes' worth. The convention was inferred
 * from the routers and generalised to a codebase that does not follow it everywhere.
 * Anyone re-deriving this needs BOTH patterns unioned, or they will reproduce the
 * undercount:
 *
 *     grep -rhoE '(forbidden|not_found|bad_request|conflict|unauthorized)\("[a-z0-9_]+"' app
 *     grep -rhoE 'detail="[a-z0-9_]+"' app
 *
 * NOT ALL 78 ARE COPY OWED. A part of the remainder is internal: configuration and
 * protocol failures like `stripe_not_configured`, `missing_bearer_token` and
 * `invalid_signature`, which a user can do nothing about and several of which arguably
 * should never surface as copy at all. Classifying them — real copy vs collapse-to-
 * generic vs should-never-reach-a-user — is batch-2 scoping with Jose, deliberately not
 * decided here. So read 78 as the size of the remaining DECISION, not as 78 sentences
 * somebody still owes.
 *
 * Copy in this map is product-approved verbatim. Adding a code means getting a line
 * written and approved, not inventing one at the call site.
 */
const DETAIL_COPY: Record<string, string> = {
  account_suspended:
    "Your account is suspended. Contact your chapter's e-board if you think that's a mistake.",
  already_member: "You're already a member of this chapter.",
  already_paid: "These dues are already paid.",
  already_registered: "You already have an account. Sign in instead.",
  campus_unverified:
    "Confirm your .edu address to unlock campus-wide posts and events. You can do it from the Home tab.",
  email_already_registered: "That email already has an account. Sign in instead.",
  email_mismatch: "Use the same email address you verified with.",
  email_send_failed: "We couldn't send the email. Try again in a moment.",
  event_canceled: "This event was canceled.",
  file_too_large: "That file is too large to upload.",
  installment_already_paid: "That installment is already paid.",
  insufficient_role: "You don't have permission to do that.",
  invalid_email: "That doesn't look like a valid email address.",
  invite_exhausted: "That invite has no uses left. Ask for a fresh one.",
  invite_expired: "That invite has expired. Ask for a fresh one.",
  invite_not_found: "That invite link isn't valid. Double-check it or ask for a new one.",
  invite_revoked: "That invite was revoked. Ask for a fresh one.",
  member_on_payment_plan: "That member is already on a payment plan.",
  no_pending_verification: "No code is active for this email. Request a new one.",
  not_a_member: "Only chapter members can do that.",
  not_author: "Only the author can change this.",
  not_on_the_guest_list: "This event is invite-only, and you're not on the guest list.",
  not_your_campus: "That belongs to a different campus, so it isn't yours to post to or open.",
  on_payment_plan: "You're already on a payment plan for these dues.",
  // Two DISTINCT backend codes deliberately sharing one string — both entries are
  // required. Dropping either because the copy looks duplicated puts a raw machine
  // code back in front of a user on whichever path still raises the other.
  payment_already_in_progress: "A payment is already in progress. Give it a moment to finish.",
  payment_in_progress: "A payment is already in progress. Give it a moment to finish.",
  poll_closed: "This poll has closed.",
  // Deliberately does NOT confirm that a block exists — same privacy class as c279,
  // where the leak was never the endpoint but what could be inferred from its answer.
  // "Can't receive messages right now" covers a block, a suspension and a deleted
  // account alike, and must not be made more "helpful".
  recipient_not_reachable: "This person can't receive messages right now.",
  unrecognized_edu_domain:
    "We don't recognize that school's email domain. Use your campus .edu address.",
  unsupported_content_type: "That file type isn't supported.",
  user_not_registered: "Your account isn't set up yet. Finish sign-up first.",
  verification_code_invalid: "That code isn't right. Check it and try again.",
  verification_expired: "That code has expired. Request a new one.",
  verification_rate_limited: "Too many attempts. Wait a few minutes, then request a new code.",
};

/**
 * The user-facing message for a failed API call: ApiError carries a
 * server-provided `.detail`, anything else gets the generic fallback.
 *
 * Exported separately from showApiError because a few call sites render this
 * same message inline (a sheet's own error line) instead of in a dialog, and
 * were each re-typing both the conditional and the fallback copy (c239). One
 * function means the dialog and the inline line can never say different things.
 *
 * An unmapped detail is still returned verbatim (c300) — the pre-existing
 * behaviour, kept deliberately: a raw code is bad copy, but it is bad copy that
 * still distinguishes "already_registered" from "invite_expired", and collapsing
 * every unmapped code into one generic line would take that away from the user AND
 * hide the remaining work.
 */
export function apiErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Try again.";
  return DETAIL_COPY[error.detail] ?? error.detail;
}

/**
 * Single-button error alert for a failed API call. ApiError carries a
 * server-provided `.detail`; anything else gets a generic fallback. c183
 * left each call site's own copy of this in place ("no shared one" per
 * CreateSheet.tsx's own comment) — c187 consolidates them here since it's
 * the same showAlert concern, verified byte-identical across every caller.
 */
export function showApiError(error: unknown, title: string): void {
  showAlert(title, apiErrorMessage(error));
}
