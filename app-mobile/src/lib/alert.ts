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
 * SCOPE, stated honestly: 69 distinct codes are raised across the backend and 58
 * client call sites render them through this function, so 67 codes still fall through
 * verbatim below. That is a real bug per remaining code, not a deliberate design —
 * it is carded separately rather than fixed here, because writing 67 pieces of
 * user-facing copy is a product decision, not a mechanical one. What this map does
 * settle is WHERE that work goes when it happens.
 */
const DETAIL_COPY: Record<string, string> = {
  campus_unverified:
    "Confirm your .edu address to unlock campus-wide posts and events. You can do it from the Home tab.",
  not_your_campus: "That belongs to a different campus, so it isn't yours to post to or open.",
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
