/** Reading failures of the E2EE calls (board c451). Pure: no imports.
 *
 * `ApiError` (src/api/client.ts) is recognised by SHAPE, not `instanceof`, so this file
 * does not import the HTTP client and the protocol logic that uses it stays runnable under
 * `npm run verify:e2ee-dm` with plain objects standing in for server errors.
 */

export interface ApiErrorLike {
  status: number;
  detail: string;
  payload: Readonly<Record<string, unknown>> | null;
}

/** The error as an HTTP failure, or null for anything else (network, timeout, native). */
export function asApiError(error: unknown): ApiErrorLike | null {
  if (typeof error !== "object" || error === null) return null;
  const e = error as { name?: unknown; status?: unknown; detail?: unknown; payload?: unknown };
  if (e.name !== "ApiError" || typeof e.status !== "number" || typeof e.detail !== "string") return null;
  const payload = typeof e.payload === "object" && e.payload !== null ? (e.payload as Record<string, unknown>) : null;
  return { status: e.status, detail: e.detail, payload };
}

/** The fixed machine code of an HTTP failure (`detail`), or null. */
export function apiCode(error: unknown): string | null {
  return asApiError(error)?.detail ?? null;
}

/** `max_generation` from a `stale_generation` 409, or null if this is not one. */
export function staleGenerationMax(error: unknown): number | null {
  const e = asApiError(error);
  if (e === null || e.status !== 409 || e.detail !== "stale_generation") return null;
  const value = e.payload?.max_generation;
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

/** True for a 409 whose `detail` is exactly `code`. */
export function isConflict(error: unknown, code: string): boolean {
  const e = asApiError(error);
  return e !== null && e.status === 409 && e.detail === code;
}

/** True for a 403 whose `detail` is exactly `code`. */
export function isForbidden(error: unknown, code: string): boolean {
  const e = asApiError(error);
  return e !== null && e.status === 403 && e.detail === code;
}

/**
 * Whether a failed call may simply be tried again later: no answer at all (offline,
 * timeout, aborted), or the server asking for patience (429) or struggling (5xx, 408).
 * Everything else is an answer: retrying the same request will get the same one.
 */
export function isRetryLater(error: unknown): boolean {
  const e = asApiError(error);
  if (e === null) return true;
  return e.status === 408 || e.status === 429 || e.status >= 500;
}

/**
 * A server that does not have the v2 API at all: the route is missing (404) or the method is
 * not routed (405). That is not a failure of this request; it means the backend this build
 * points at has not been deployed with E2EE yet, so the right answer is "not available yet",
 * not an error and not a retry. Used only for the calls that start a device
 * (POST /v2/devices, GET /v2/users/{id}/devices): a 404 anywhere else means something else.
 */
export function isServerWithoutV2(error: unknown): boolean {
  const e = asApiError(error);
  return e !== null && (e.status === 404 || e.status === 405);
}

/**
 * 423 `account_deletion_in_progress`: an account the write touches (the caller's own, or
 * for a message or key claim any participant's) is being deleted. Every v2 WRITE is refused
 * until the deletion finishes, and it does not finish by itself becoming allowed again, so
 * this is an answer to act on (stop, say why), not a reason to retry.
 */
export function isAccountDeleting(error: unknown): boolean {
  const e = asApiError(error);
  return e !== null && e.status === 423 && e.detail === "account_deletion_in_progress";
}

/** A code that is safe to print: lowercase words and underscores only, never free text. */
export function safeCode(value: string): string {
  return /^[a-z0-9_]{1,64}$/.test(value) ? value : "error";
}

/** The 403s that mean THIS phone's device cannot act (revoked, not approved, not ours). */
const DEVICE_UNUSABLE = new Set(["device_revoked", "device_not_approved", "not_your_device", "device_suite_unsupported"]);

export function isDeviceUnusable(error: unknown): boolean {
  const e = asApiError(error);
  return e !== null && e.status === 403 && DEVICE_UNUSABLE.has(e.detail);
}

/** The native core's own error code (`E2EEError.code`), or null for anything else. */
export function nativeCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null) return null;
  const e = error as { name?: unknown; code?: unknown };
  return e.name === "E2EEError" || e.name === "E2EEUnavailableError"
    ? (typeof e.code === "string" ? e.code : null)
    : null;
}

/**
 * Core failures that are a property of ONE received message and will not change on a
 * retry. A message that fails with one of these gets a "couldn't decrypt" placeholder.
 * Every other code (locked, storage_failed, internal, ...) is about the device or the
 * disk, says nothing about the message, and stops the sync so it can run again later.
 */
const PERMANENT_DECRYPT_FAILURES = new Set([
  "decrypt_failed",
  "invalid_envelope",
  "envelope_mismatch",
  "replay",
  "invalid_signature",
  "invalid_input",
  "body_invalid",
  "body_too_large",
  "sender_mismatch",
  "invalid_key",
  "unknown_key",
]);

export function isPermanentDecryptFailure(code: string | null): boolean {
  return code !== null && PERMANENT_DECRYPT_FAILURES.has(code);
}
