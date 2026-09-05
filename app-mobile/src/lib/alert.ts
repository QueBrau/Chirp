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
 * SCOPE, RE-DERIVED at batch 2 rather than carried forward (c315). The backend raises
 * 113 distinct codes; 97 are mapped below and 16 collapse to one line via SYSTEM_CODES,
 * so all 113 are accounted for, across the 58 client call sites that render them through
 * this function.
 *
 * RE-DERIVE, NEVER INHERIT, and diff the SETS rather than the totals: batch 2's lists
 * were built against a 112-code census and also totalled 112, so the arithmetic checked
 * out while `query_too_short` had been added to the backend (+1) and `invalid_base64`
 * dropped from the lists (−1) — one in, one out, checksum unchanged, one code silently
 * uncovered. A matching total is not a covered set.
 *
 * THE NUMBERS HERE USED TO BE WRONG in a second way, recorded rather than quietly fixed.
 * c300 said "69 raised, 67 remaining" because that census grepped only the `forbidden()`
 * / `not_found()` / `conflict()` / `bad_request()` / `unauthorized()` helpers and never
 * checked whether services and middleware used them. They do not — campus verification,
 * storage, email and the auth middleware raise `HTTPException(detail="...")` directly.
 * A convention inferred from the routers, generalised to a codebase that does not follow
 * it everywhere. Anyone re-deriving needs BOTH patterns unioned:
 *
 *     grep -rhoE '(forbidden|not_found|bad_request|conflict|unauthorized)\("[a-z0-9_]+"' app
 *     grep -rhoE 'detail="[a-z0-9_]+"' app
 *
 * THE PER-SCREEN SEAM (c327, Jose's ruling: per-screen copy WINS). Some screens map these
 * same codes themselves, and their wording is the deliberate winner for their flow, being
 * richer and status-aware in a way a shared per-code table cannot be. This map is the
 * fallback for every OTHER call site, so two strings for one code is intended here, not
 * drift to reconcile.
 *
 * FOUR mappers currently do this, which is more than c327 recorded — the fourth turned up
 * only because a batch-2 spot-check went looking for a code it could actually trigger:
 *
 *     (auth)/join-chapter.tsx      the join()* catch chain      c327
 *     (auth)/verify-campus.tsx     sendError / redeemError      c327 / c158
 *     (tabs)/messages/new.tsx      searchUsersErrorMessage      c315 batch 2
 *     (tabs)/messages/new.tsx      createConversationErrorMessage — c164, whose own
 *                                  comment says it exists so apiErrorMessage will not
 *                                  surface a raw code
 *
 * So do not read an entry below as what a user sees on those flows; check the screen.
 *
 * Copy in this map is product-approved verbatim. Adding a code means getting a line
 * written and approved, not inventing one at the call site.
 */
const DETAIL_COPY: Record<string, string> = {
  account_suspended:
    "Your account is suspended. Contact your chapter's e-board if you think that's a mistake.",
  already_blocked: "You've already blocked this person.",
  already_decided: "This spend request was already decided.",
  already_member: "You're already a member of this chapter.",
  already_paid: "These dues are already paid.",
  already_registered: "You already have an account. Sign in instead.",
  already_removed: "They're no longer a member of this chapter.",
  already_suspended: "They're already suspended.",
  alumni_or_eboard_only: "Only alumni and e-board can do that.",
  alumni_profile_not_found: "That alumni profile no longer exists.",
  big_and_little_identical: "Big and little can't be the same person.",
  block_not_found: "This person isn't blocked.",
  campus_not_found: "We couldn't find that campus.",
  campus_unverified:
    "Confirm your .edu address to unlock campus-wide posts and events. You can do it from the Home tab.",
  cannot_block_self: "You can't block yourself.",
  chapter_not_found: "We couldn't find that chapter.",
  chapter_not_onboarded: "This chapter isn't set up yet.",
  chirp_not_found: "That post is gone. It may have been deleted.",
  comment_not_found: "That comment is gone. It may have been deleted.",
  correction_requires_corrects_entry_id: "Pick the entry this correction applies to.",
  correction_target_is_correction: "You can't correct a correction. Correct the original entry.",
  corrects_entry_not_in_chapter: "That entry belongs to a different chapter.",
  device_not_found: "We couldn't find that device.",
  device_revoked: "This device's access was revoked. Sign in again.",
  display_name_cannot_be_cleared: "Your display name can't be empty.",
  dues_cycle_not_found: "That dues cycle no longer exists.",
  dues_payment_plan_not_found: "That payment plan no longer exists.",
  dues_plan_installment_not_found: "That installment no longer exists.",
  edge_not_found: "That pairing no longer exists.",
  email_already_registered: "That email already has an account. Sign in instead.",
  email_mismatch: "Use the same email address you verified with.",
  email_send_failed: "We couldn't send the email. Try again in a moment.",
  ends_at_must_be_after_starts_at: "The end time has to be after the start time.",
  event_canceled: "This event was canceled.",
  event_not_found: "That event is gone. It may have been deleted.",
  family_not_in_chapter: "They need to be a member of this chapter first.",
  file_too_large: "That file is too large to upload.",
  installment_already_paid: "That installment is already paid.",
  installment_count_mismatch: "The number of installments doesn't match the plan.",
  installments_must_sum_to_cycle_amount: "Installments have to add up to the cycle total.",
  insufficient_role: "You don't have permission to do that.",
  invalid_email: "That doesn't look like a valid email address.",
  invalid_window: "The start of the range has to be before the end.",
  invite_exhausted: "That invite has no uses left. Ask for a fresh one.",
  invite_expired: "That invite has expired. Ask for a fresh one.",
  invite_expiry_in_past: "That expiration date is in the past. Pick a future one.",
  invite_not_found: "That invite link isn't valid. Double-check it or ask for a new one.",
  invite_revoked: "That invite was revoked. Ask for a fresh one.",
  job_not_found: "That job post is gone. It may have been taken down.",
  lineage_cycle: "That pairing would create a loop in the family tree.",
  lineage_target_not_in_chapter: "They need to be a member of this chapter first.",
  little_already_has_big: "They already have a big.",
  media_finalize_failed: "That upload didn't finish. Try attaching it again.",
  media_token_expired: "That upload took too long. Attach it again.",
  media_upload_not_found: "That upload didn't go through. Attach it again.",
  meeting_not_found: "That meeting is gone. It may have been deleted.",
  member_on_payment_plan: "That member is already on a payment plan.",
  membership_not_found: "They're not a member of this chapter.",
  message_not_found: "That message is gone.",
  no_pending_verification: "No code is active for this email. Request a new one.",
  not_a_member: "Only chapter members can do that.",
  not_author: "Only the author can change this.",
  not_author_or_president: "Only the author or the president can do that.",
  // not_device_owner / not_your_device and family_not_in_chapter /
  // lineage_target_not_in_chapter are intentional duplicate strings (c315 batch 2):
  // distinct backend codes that mean the same thing to a person. Same rule as the
  // payment pair above — the duplication is the point, not an oversight to collapse.
  not_device_owner: "That device belongs to a different account.",
  not_on_the_guest_list: "This event is invite-only, and you're not on the guest list.",
  not_poster: "Only the poster can do that.",
  not_suspended: "They're not suspended.",
  not_the_host: "Only the event host can do that.",
  not_your_campus: "That belongs to a different campus, so it isn't yours to post to or open.",
  not_your_device: "That device belongs to a different account.",
  on_payment_plan: "You're already on a payment plan for these dues.",
  only_little_can_confirm: "Only the little can confirm this pairing.",
  option_not_found: "That poll option no longer exists.",
  // Two DISTINCT backend codes deliberately sharing one string — both entries are
  // required. Dropping either because the copy looks duplicated puts a raw machine
  // code back in front of a user on whichever path still raises the other.
  payment_already_in_progress: "A payment is already in progress. Give it a moment to finish.",
  payment_in_progress: "A payment is already in progress. Give it a moment to finish.",
  payment_intent_conflict: "A payment for this is already being processed. Give it a moment.",
  plan_not_active: "That payment plan isn't active anymore.",
  platform_admin_required: "Only platform admins can do that.",
  poll_closed: "This poll has closed.",
  poll_has_ballots: "This poll already has votes, so it can't be changed.",
  poll_not_found: "That poll is gone. It may have been deleted.",
  post_not_found: "That post is gone. It may have been deleted.",
  query_too_short: "Type a few more characters to search.",
  // Deliberately does NOT confirm that a block exists — same privacy class as c279,
  // where the leak was never the endpoint but what could be inferred from its answer.
  // "Can't receive messages right now" covers a block, a suspension and a deleted
  // account alike, and must not be made more "helpful".
  recipient_not_reachable: "This person can't receive messages right now.",
  report_already_resolved: "This report was already resolved.",
  report_not_found: "That report no longer exists.",
  spend_approval_not_found: "That spend request no longer exists.",
  touse_and_bouse_must_differ: "Both sides of the trade can't be the same chapter.",
  unknown_house_for_this_campus: "We don't have that house on this campus yet.",
  unknown_user_in_invite_list: "Someone on that list doesn't have an account yet.",
  unrecognized_edu_domain:
    "We don't recognize that school's email domain. Use your campus .edu address.",
  unsupported_content_type: "That file type isn't supported.",
  user_not_found: "We couldn't find that person.",
  user_not_registered: "Your account isn't set up yet. Finish sign-up first.",
  verification_code_invalid: "That code isn't right. Check it and try again.",
  verification_expired: "That code has expired. Request a new one.",
  verification_rate_limited: "Too many attempts. Wait a few minutes, then request a new code.",
};

/**
 * Codes a user can do nothing about, collapsed to one line (c315 batch 2, Jose-approved).
 *
 * These are configuration and protocol failures — an unset Stripe key, a missing bearer
 * token, a malformed signature, a media URL the server will not accept. Telling someone
 * "stripe_not_configured" is worse than telling them nothing: it is a machine code AND
 * an instruction they cannot act on. One honest sentence is the whole of what there is
 * to say to them.
 *
 * CHECKED BEFORE DETAIL_COPY, deliberately. Membership in this set is a statement that
 * the code has no useful per-code copy, so an entry here must never be shadowed by
 * someone later adding a specific line for the same code — the collapse is the ruling,
 * not a placeholder.
 *
 * THE RAW CODE SURVIVES for logs: this only changes what apiErrorMessage RENDERS.
 * ApiError still carries `.detail`, so anything reading the error programmatically —
 * or anyone reading a console — still gets `missing_stripe_signature` and not this
 * sentence. Collapsing the copy must not collapse the diagnosis.
 */
const SYSTEM_CODES: ReadonlySet<string> = new Set([
  "app_public_base_url_not_configured",
  "dues_installment_requires_plan_route",
  "email_not_configured",
  "invalid_base64",
  "invalid_media_token",
  "invalid_media_url",
  "invalid_payload",
  "invalid_signature",
  "invalid_token",
  "media_not_configured",
  "media_url_too_long",
  "missing_bearer_token",
  "missing_debug_uid",
  "missing_stripe_signature",
  "stripe_not_configured",
  "too_many_media_urls",
]);

const SYSTEM_MESSAGE = "Something went wrong on our end. Try again in a moment.";

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
  if (SYSTEM_CODES.has(error.detail)) return SYSTEM_MESSAGE;
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
