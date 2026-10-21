/** Conversions between the server's snake_case v2 shapes and the native core's records
 * (board c451). Pure functions; every field that crosses is copied by name, nothing is
 * defaulted or guessed, and a directory row that does not add up is refused rather than
 * repaired (the core would reject it anyway, with a less useful error).
 */

import type { ClaimedKeyOut, DeviceRegisterBody, DirectoryDeviceOut, KeyUploadBody, LegBody, SignedKeyBody } from "../api/e2ee";
import type { CreatedDevice, DirectoryDevice, Leg, SignedKey } from "./native";
import { DEVICE_LABEL, E2EE_SUITE } from "./e2eeTypes";

/** A directory row the server sent that cannot be a valid device. */
export class DirectoryShapeError extends Error {
  constructor() {
    super("directory_invalid");
    this.name = "DirectoryShapeError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/**
 * One server directory row as the core's `DirectoryDevice`. A non-root device must carry
 * its approver's identity and signature; a root must carry neither. Anything else is a
 * malformed row. The core still verifies every signature; this only maps names.
 */
export function toDirectoryDevice(userId: string, row: DirectoryDeviceOut): DirectoryDevice {
  const hasApprover = row.approved_by_device_id !== null && row.approved_by_device_id !== undefined;
  if (hasApprover) {
    if (!row.approval_signature_b64 || !row.approver_identity_ed25519_b64) throw new DirectoryShapeError();
  } else if (row.approval_signature_b64 || row.approver_identity_ed25519_b64) {
    throw new DirectoryShapeError();
  }
  return {
    userId,
    deviceId: row.device_id,
    generation: row.generation,
    identityCurve25519: row.identity_curve25519_b64,
    identityEd25519: row.identity_ed25519_b64,
    bindingSignature: row.binding_signature_b64,
    approval: hasApprover
      ? { approverEd25519: row.approver_identity_ed25519_b64 as string, signature: row.approval_signature_b64 as string }
      : null,
  };
}

/** A key the server handed out by `POST /v2/keys/claim`, as the core's `SignedKey`. */
export function toSignedKey(claimed: ClaimedKeyOut): SignedKey {
  return {
    keyId: claimed.key_id,
    kind: claimed.kind,
    publicKey: claimed.public_key_b64,
    signature: claimed.signature_b64,
  };
}

export function keyBody(key: SignedKey): SignedKeyBody {
  return { key_id: key.keyId, public_key_b64: key.publicKey, signature_b64: key.signature };
}

/** The `POST /v2/devices` body for a device the core just created. */
export function registrationBody(created: CreatedDevice, generation: number): DeviceRegisterBody {
  return {
    device_label: DEVICE_LABEL,
    suite: E2EE_SUITE,
    generation,
    identity_curve25519_b64: created.identity.curve25519,
    identity_ed25519_b64: created.identity.ed25519,
    binding_signature_b64: created.bindingSignature,
    one_time_keys: created.oneTimeKeys.map(keyBody),
    fallback_key: keyBody(created.fallbackKey),
  };
}

/** Split signed keys into the upload body: one-time keys, plus the fallback if present. */
export function uploadBody(keys: readonly SignedKey[]): KeyUploadBody {
  const fallback = keys.find((key) => key.kind === "fallback");
  return {
    one_time_keys: keys.filter((key) => key.kind === "one_time").map(keyBody),
    ...(fallback ? { fallback_key: keyBody(fallback) } : {}),
  };
}

export function legBody(leg: Leg): LegBody {
  return { recipient_device_id: leg.recipientDeviceId, olm_type: leg.olmType, ciphertext_b64: leg.ciphertext };
}

/** Compare two server positions `(created_at, id)`: negative when `a` is older. */
export function comparePosition(a: { created_at: string; id: string }, b: { created_at: string; id: string }): number {
  const fraction = (time: string) =>
    Number((/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(time)?.[1] ?? "").padEnd(6, "0").slice(3, 6));
  return (
    Date.parse(a.created_at) - Date.parse(b.created_at) ||
    fraction(a.created_at) - fraction(b.created_at) ||
    (a.id === b.id ? 0 : a.id < b.id ? -1 : 1)
  );
}
