/**
 * Content length caps, mirrored from backend/app/core/validation.py (board c245).
 *
 * THE BACKEND IS THE SOURCE OF TRUTH. It 422s an over-length body regardless of what
 * this file says; these exist so a student finds out while typing instead of after
 * hitting send (c251). There is no build-time path from Python to the Expo bundle, so
 * this is a hand-copy - but not one that can drift silently:
 * `npm run verify:content-limits` extracts the real numbers from core/validation.py
 * and fails if they disagree with these, and it runs in CI on every PR. Change the
 * backend cap and this file goes red until it is updated.
 *
 * MEASURE THE TRIMMED STRING. Every composer sends `body.trim()`, so the trimmed
 * length is what the server actually validates. Counting the raw value instead would
 * let the client accept a body the server then rejects, which is precisely the
 * after-the-fact 422 this card exists to remove.
 *
 * MAX_REASON_LENGTH (1000) is deliberately NOT mirrored here. No mobile surface
 * collects a free-text moderation reason: every reason sent from this client is a
 * preset - "Spam" / "Harassment" / "Inappropriate" from MediaPostCard's REPORT_REASONS
 * and the chirps sheet, or the literal "Reviewed, no action needed" on dismiss. The
 * longest is 26 characters against a 1000 cap, so a counter there would be UI for an
 * unreachable state. verify-content-limits.mjs knows this constant is intentionally
 * unmirrored and will fail if a NEW backend cap appears that is neither mirrored nor
 * listed there, so adding one forces a decision rather than being silently ignored.
 */

/** ChirpCreate.body — the anonymous campus composer. */
export const MAX_CHIRP_BODY_LENGTH = 2_000;

/** PostCreate.body and CampusPostCreate.body — the CreateSheet composer. */
export const MAX_POST_BODY_LENGTH = 10_000;

/** PostCommentCreate.body — the comments sheet composer. */
export const MAX_COMMENT_BODY_LENGTH = 2_000;

/**
 * How close to the cap the counter starts showing: the last 10% of the allowance.
 * A counter pinned on screen from the first character reads as a homework
 * assignment; one that appears as you approach the limit reads as a warning.
 */
export const COUNTER_REVEAL_FRACTION = 0.1;

/** Characters left before the cap, negative once over it. Trimmed, as the server sees it. */
export function charsRemaining(value: string, limit: number): number {
  return limit - value.trim().length;
}

/** True once the counter should be visible — inside the last COUNTER_REVEAL_FRACTION, or over. */
export function shouldShowCounter(value: string, limit: number): boolean {
  return charsRemaining(value, limit) <= Math.ceil(limit * COUNTER_REVEAL_FRACTION);
}

/** True when the body is too long for the server to accept. */
export function isOverLimit(value: string, limit: number): boolean {
  return charsRemaining(value, limit) < 0;
}
