"""E2EE v2 schemas: device directory, key claim, per-device message legs (board card c444).

Every input is bounded at the HTTP boundary: key and signature fields are exact-width
base64 (reusing schemas.e2ee's validators, so the width rules cannot drift between v1
and v2), key ids are integers that fit the BIGINT column, and every list has a ceiling.
Output models are built by the routers from ORM rows; nothing here parses ciphertext.
"""

import base64
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.core.e2ee_v2 import MAX_GENERATION, MAX_KEY_ID
from app.core.validation import MAX_CIPHERTEXT_B64_LENGTH
from app.schemas.base import _Schema
from app.schemas.e2ee import (
    MAX_DEVICE_LABEL_LENGTH,
    MAX_PREKEY_BATCH,
    PublicKeyInput,
    SignatureInput,
)

# vodozemac key ids are u64; the API keeps them below 2**63 so they fit BIGINT.
V2KeyId = Annotated[int, Field(ge=0, le=MAX_KEY_ID)]
# 0 is deliberately accepted here and refused by the router as `stale_generation` (a
# first device must use generation >= 1): one rule, one error, one place.
Generation = Annotated[int, Field(ge=0, le=MAX_GENERATION)]

# Most devices one POST /v2/keys/claim may name. A DM needs the other person's devices
# plus the sender's other devices: at most (5 - 1) + 5 = 9, so 10 = 2 x MAX_ACTIVE_DEVICES
# is the ceiling. Same number bounds the legs of one message below.
MAX_CLAIM_DEVICES = 10
# Legs per message. Equals 2 x routers.keys.MAX_ACTIVE_DEVICES (pinned by a test): v2
# transport is DM-only in c444, so a message has exactly two members' devices to reach.
# At 65,536 base64 characters per leg this keeps one request near 650 KB.
MAX_V2_LEGS_PER_MESSAGE = 10
# Unconsumed one-time keys one device may hold at once. The client tops up below 50 of
# 100, so 200 is two full pools of headroom; a larger pool is only a bigger blast radius
# if the device's key store is ever lost.
MAX_AVAILABLE_ONE_TIME_KEYS = 200

ENVELOPE_VERSION_1 = 1


# ---- key and device inputs ----


class SignedKeyInput(_Schema):
    """One signed one-time or fallback key. The signed bytes are core.e2ee_v2.one_time_key_message."""

    key_id: V2KeyId
    public_key_b64: PublicKeyInput
    signature_b64: SignatureInput


class DeviceRegisterV2(_Schema):
    """Body for POST /v2/devices.

    `suite` is a free string here and checked against the supported set by the router, so
    an unknown suite gets the fixed `unsupported_suite` code rather than a generic 422.
    A fallback key is mandatory: it is what makes every approved device claimable even
    when its one-time pool is empty.
    """

    device_label: str | None = Field(default=None, max_length=MAX_DEVICE_LABEL_LENGTH)
    suite: str = Field(min_length=1, max_length=64)
    generation: Generation
    identity_curve25519_b64: PublicKeyInput
    identity_ed25519_b64: PublicKeyInput
    binding_signature_b64: SignatureInput
    one_time_keys: list[SignedKeyInput] = Field(default_factory=list, max_length=MAX_PREKEY_BATCH)
    fallback_key: SignedKeyInput


class DeviceApproveV2(_Schema):
    """Body for POST /v2/devices/{id}/approve."""

    approver_device_id: uuid.UUID
    approval_signature_b64: SignatureInput


class KeyUploadV2(_Schema):
    """Body for POST /v2/devices/{id}/keys: top up one-time keys and/or replace the fallback."""

    one_time_keys: list[SignedKeyInput] = Field(default_factory=list, max_length=MAX_PREKEY_BATCH)
    fallback_key: SignedKeyInput | None = None


class KeyClaimRequest(_Schema):
    """Body for POST /v2/keys/claim. Duplicates are refused by the router."""

    device_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_CLAIM_DEVICES)


# ---- device and key outputs ----


def b64(value: bytes | None) -> str | None:
    """Base64 for JSON output; None passes through."""
    return None if value is None else base64.b64encode(value).decode("ascii")


class DeviceV2Out(_Schema):
    """A device as its OWNER sees it (register / approve / revoke responses)."""

    id: uuid.UUID
    user_id: uuid.UUID
    device_label: str | None = None
    suite: str
    generation: int
    identity_curve25519_b64: str
    identity_ed25519_b64: str
    binding_signature_b64: str
    # False = pending: not in the directory, not claimable, cannot send or receive legs.
    approved: bool
    approved_at: datetime | None = None
    # None on an approved device means ROOT (the account's first device); only
    # meaningful together with `approved`.
    approved_by_device_id: uuid.UUID | None = None
    approval_signature_b64: str | None = None
    created_at: datetime
    revoked_at: datetime | None = None


class KeyCountV2Out(_Schema):
    """Unconsumed keys per kind. fallback is 0 or 1: a device has at most one fallback key."""

    device_id: uuid.UUID
    one_time_keys_available: int
    fallback_keys_available: int


class DirectoryDeviceOut(_Schema):
    """One approved, unrevoked v2 device in a user's directory. Reading it claims nothing."""

    device_id: uuid.UUID
    suite: str
    generation: int
    identity_curve25519_b64: str
    identity_ed25519_b64: str
    binding_signature_b64: str
    created_at: datetime
    # Approval chain. approved_by_device_id None = root device. The approver's Ed25519
    # identity is included so the chain stays verifiable after the approver is revoked
    # (revoked devices are not listed); a client must still check it against its own pin.
    approved_by_device_id: uuid.UUID | None = None
    approval_signature_b64: str | None = None
    approver_identity_ed25519_b64: str | None = None


class UserDirectoryOut(_Schema):
    user_id: uuid.UUID
    devices: list[DirectoryDeviceOut]


class ClaimedKeyOut(_Schema):
    device_id: uuid.UUID
    key_id: int
    kind: Literal["one_time", "fallback"]
    public_key_b64: str
    signature_b64: str


class KeyClaimOut(_Schema):
    """Claimed keys in the order the device ids were requested."""

    keys: list[ClaimedKeyOut]


# ---- message transport ----


class LegCreate(_Schema):
    """One ciphertext leg. olm_type: 0 = Olm prekey message, 1 = normal message.

    Bounded as base64 text by the SAME cap v1 uses for one ciphertext; the server never
    parses the bytes (SPEC section 8.1).
    """

    recipient_device_id: uuid.UUID
    olm_type: Literal[0, 1]
    ciphertext_b64: str = Field(min_length=1, max_length=MAX_CIPHERTEXT_B64_LENGTH)


class MessageCreateV2(_Schema):
    """Body for POST /v2/conversations/{id}/messages."""

    sender_device_id: uuid.UUID
    # The client-generated logical id. With sender_device_id it is the idempotency key.
    client_message_id: uuid.UUID
    envelope_version: int = ENVELOPE_VERSION_1
    legs: list[LegCreate] = Field(min_length=1, max_length=MAX_V2_LEGS_PER_MESSAGE)


class MessageV2Out(_Schema):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_device_id: uuid.UUID
    client_message_id: uuid.UUID
    envelope_version: int
    created_at: datetime


class MessageLegOut(_Schema):
    olm_type: int
    ciphertext_b64: str


class MessageHistoryV2Out(MessageV2Out):
    """A history row: the message plus ONLY the calling device's leg."""

    leg: MessageLegOut
