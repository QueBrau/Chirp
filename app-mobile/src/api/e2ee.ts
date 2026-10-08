/** E2EE v2 API: device directory, key claim, per-device message legs (board c451).
 *
 * routers/keys_v2.py and routers/messages_v2.py. Everything here is a thin typed wrapper
 * over `request`; the protocol (what to send, when to retry) lives in src/crypto/. Bodies
 * go out in the server's snake_case; `src/crypto/e2eeMapping.ts` converts to and from the
 * camelCase records the native core uses.
 *
 * The server never sees plaintext. A message is a set of ciphertext legs, one per
 * recipient device, and every `*_b64` field is an opaque blob it only length-checks.
 *
 * The suite and envelope version constants are in src/crypto/e2eeTypes.ts, and the readers
 * for the error shapes below are in src/crypto/e2eeErrors.ts (both kept free of this
 * module's imports so the protocol logic can be executed under node).
 *
 * The fixed error codes a caller has to handle ride in `ApiError.detail`, with extra
 * recovery data beside it in `ApiError.payload`:
 *   409 stale_generation        + max_generation
 *   409 device_list_mismatch    + device_ids
 *   409 device_unavailable      + device_ids   (key claim; nothing was claimed)
 *   409 client_message_id_reused, no_recipient_devices, e2ee_v2_requires_dm,
 *       identity_already_registered, key_id_reused, one_time_key_pool_full
 *   403 recipient_not_reachable (= not allowed; never retry), device_revoked,
 *       device_not_approved, not_your_device, device_suite_unsupported
 */

import { request, type RequestOptions } from "./client";

export type E2eeRequestOptions = Pick<RequestOptions, "operation" | "signal" | "timeoutMs">;

// ---- bodies ----------------------------------------------------------------------

export interface SignedKeyBody {
  key_id: number;
  public_key_b64: string;
  signature_b64: string;
}

export interface DeviceRegisterBody {
  device_label?: string | null;
  suite: string;
  generation: number;
  identity_curve25519_b64: string;
  identity_ed25519_b64: string;
  binding_signature_b64: string;
  one_time_keys: SignedKeyBody[];
  fallback_key: SignedKeyBody;
}

export interface KeyUploadBody {
  one_time_keys: SignedKeyBody[];
  fallback_key?: SignedKeyBody | null;
}

export interface LegBody {
  recipient_device_id: string;
  /** 0 = Olm prekey message, 1 = normal. */
  olm_type: 0 | 1;
  ciphertext_b64: string;
}

export interface SendMessageV2Body {
  sender_device_id: string;
  client_message_id: string;
  envelope_version: number;
  legs: LegBody[];
}

// ---- responses -------------------------------------------------------------------

/** A device as its owner sees it. `approved: false` is PENDING: not in any directory. */
export interface DeviceV2Out {
  id: string;
  user_id: string;
  device_label: string | null;
  suite: string;
  generation: number;
  identity_curve25519_b64: string;
  identity_ed25519_b64: string;
  binding_signature_b64: string;
  approved: boolean;
  approved_at: string | null;
  approved_by_device_id: string | null;
  approval_signature_b64: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface KeyCountV2Out {
  device_id: string;
  one_time_keys_available: number;
  fallback_keys_available: number;
}

/** One approved, unrevoked device in a user's directory. Reading it claims nothing. */
export interface DirectoryDeviceOut {
  device_id: string;
  suite: string;
  generation: number;
  identity_curve25519_b64: string;
  identity_ed25519_b64: string;
  binding_signature_b64: string;
  created_at: string;
  /** Null = root device. */
  approved_by_device_id: string | null;
  approval_signature_b64: string | null;
  approver_identity_ed25519_b64: string | null;
}

export interface UserDirectoryOut {
  user_id: string;
  devices: DirectoryDeviceOut[];
}

export interface ClaimedKeyOut {
  device_id: string;
  key_id: number;
  kind: "one_time" | "fallback";
  public_key_b64: string;
  signature_b64: string;
}

export interface KeyClaimOut {
  /** In the order the device ids were requested. */
  keys: ClaimedKeyOut[];
}

export interface MessageV2Out {
  id: string;
  conversation_id: string;
  sender_device_id: string;
  client_message_id: string;
  envelope_version: number;
  created_at: string;
}

export interface MessageHistoryV2Out extends MessageV2Out {
  /** Only the calling device's leg. */
  leg: { olm_type: number; ciphertext_b64: string };
}

// ---- calls -----------------------------------------------------------------------
//
// Each call spells out its transport fields (operation, signal, timeoutMs, method) instead of
// spreading `options`: scripts/verify-contract.mjs reads these call sites statically and
// refuses a spread, because a spread could hide the method or the query keys.

/** Register a v2 device. The account's first device comes back approved; later ones pending. */
export async function registerDeviceV2(body: DeviceRegisterBody, options: E2eeRequestOptions = {}): Promise<DeviceV2Out> {
  return request<DeviceV2Out>("/v2/devices", {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs, method: "POST", body,
  });
}

export async function uploadKeysV2(deviceId: string, body: KeyUploadBody, options: E2eeRequestOptions = {}): Promise<KeyCountV2Out> {
  return request<KeyCountV2Out>(`/v2/devices/${deviceId}/keys`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs, method: "POST", body,
  });
}

export async function getKeyCountV2(deviceId: string, options: E2eeRequestOptions = {}): Promise<KeyCountV2Out> {
  return request<KeyCountV2Out>(`/v2/devices/${deviceId}/keys/count`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
  });
}

/** The user's approved devices. Allowed for yourself and your current conversation mates only. */
export async function getUserDirectoryV2(userId: string, options: E2eeRequestOptions = {}): Promise<UserDirectoryOut> {
  return request<UserDirectoryOut>(`/v2/users/${userId}/devices`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
  });
}

/** Claim one key per named device (a one-time key, else the fallback). All or nothing. */
export async function claimKeysV2(deviceIds: readonly string[], options: E2eeRequestOptions = {}): Promise<KeyClaimOut> {
  return request<KeyClaimOut>("/v2/keys/claim", {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs, method: "POST",
    body: { device_ids: deviceIds },
  });
}

export async function revokeDeviceV2(deviceId: string, options: E2eeRequestOptions = {}): Promise<DeviceV2Out> {
  return request<DeviceV2Out>(`/v2/devices/${deviceId}`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs, method: "DELETE",
  });
}

/** Store one message as one ciphertext leg per recipient device. Idempotent on client_message_id. */
export async function sendMessageV2(conversationId: string, body: SendMessageV2Body, options: E2eeRequestOptions = {}): Promise<MessageV2Out> {
  return request<MessageV2Out>(`/v2/conversations/${conversationId}/messages`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs, method: "POST", body,
  });
}

export interface ListMessagesV2Options extends E2eeRequestOptions {
  /** created_at cursor: messages older than this. */
  before?: string;
  /** id tie-break for rows sharing `before`'s created_at. */
  beforeId?: string;
  limit?: number;
}

/** History for ONE device, newest first, each row carrying only that device's leg. */
export async function listMessagesV2(
  conversationId: string,
  deviceId: string,
  options: ListMessagesV2Options = {},
): Promise<MessageHistoryV2Out[]> {
  return request<MessageHistoryV2Out[]>(`/v2/conversations/${conversationId}/messages`, {
    operation: options.operation, signal: options.signal, timeoutMs: options.timeoutMs,
    query: { device_id: deviceId, before: options.before, before_id: options.beforeId, limit: options.limit },
  });
}
