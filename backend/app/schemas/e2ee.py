"""E2EE key-directory schemas: devices, signed/one-time prekeys, prekey bundles."""

import base64
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AliasChoices, BeforeValidator, Field

from app.schemas.base import _Schema


def _to_b64(value: object) -> object:
    """Encode raw bytes (from ORM BYTEA columns) to a base64 str; pass strings through."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


Base64Str = Annotated[str, BeforeValidator(_to_b64)]


# ---- prekey inputs ----


class SignedPrekeyCreate(_Schema):
    key_id: int
    public_key_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


class OneTimePrekeyCreate(_Schema):
    key_id: int
    public_key_b64: str = Field(min_length=1)


class KyberPrekeyCreate(_Schema):
    """A single Kyber (PQXDH) prekey — used for both the last-resort slot and one-time batch."""

    key_id: int
    public_key_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


# ---- device registration ----


# Bounded because POST /devices and POST /devices/{device_id}/prekeys write one row
# per element (routers/keys.py's session.add_all calls): an unbounded list lets the
# caller decide how much work one request costs. Rate limiting does not close this -
# it caps how OFTEN a caller may post, not how many rows a single permitted post
# inserts, so the two guards are complements rather than substitutes (board c263/c264).
#
# 200 is twice SPEC 6.1's "~100 one-time prekeys" registration batch, which is also the
# largest batch the client ever sends: app-mobile/src/crypto/keys.ts sets
# INITIAL_ONE_TIME_PREKEY_COUNT = 100 and replenishes only when the server reports
# fewer than PREKEY_REPLENISH_THRESHOLD = 20 unconsumed. So a full registration and a
# full top-up both fit with the whole batch again to spare, while a payload that writes
# tens of thousands of rows does not. Deliberately generous: the cost of a cap that is
# slightly too high is nothing, and the cost of one that is too low is a device that
# cannot register.
MAX_PREKEY_BATCH = 200


class DeviceCreate(_Schema):
    """Body for POST /devices — identity key + signed prekey + one-time prekey batch.

    Kyber fields are optional (nullable path): a device may register without them and
    the prekey bundle will simply return `kyber_prekey: null` for that device — existing
    clients/tests that predate PQXDH support keep working unchanged.
    """

    device_label: str | None = None
    registration_id: int
    identity_key_b64: str = Field(min_length=1)
    signed_prekey: SignedPrekeyCreate
    one_time_prekeys: list[OneTimePrekeyCreate] = Field(
        default_factory=list, max_length=MAX_PREKEY_BATCH
    )
    kyber_last_resort: KyberPrekeyCreate | None = None
    kyber_one_time: list[KyberPrekeyCreate] = Field(
        default_factory=list, max_length=MAX_PREKEY_BATCH
    )


class DeviceOut(_Schema):
    id: uuid.UUID
    user_id: uuid.UUID
    device_label: str | None = None
    registration_id: int
    identity_key_b64: Base64Str = Field(
        validation_alias=AliasChoices("identity_key_b64", "identity_key")
    )
    created_at: datetime
    revoked_at: datetime | None = None


# ---- prekey upload batch (POST /devices/{device_id}/prekeys) ----


class PrekeyUpload(_Schema):
    """Replenish one-time prekeys and/or rotate the signed prekey.

    Kyber fields are optional: `kyber_last_resort` rotates the last-resort Kyber prekey
    (a new row is inserted; the previous last-resort row is simply superseded, never
    consumed), `kyber_one_time` tops up the one-time Kyber pool.
    """

    signed_prekey: SignedPrekeyCreate | None = None
    one_time_prekeys: list[OneTimePrekeyCreate] = Field(
        default_factory=list, max_length=MAX_PREKEY_BATCH
    )
    kyber_last_resort: KyberPrekeyCreate | None = None
    kyber_one_time: list[KyberPrekeyCreate] = Field(
        default_factory=list, max_length=MAX_PREKEY_BATCH
    )


PrekeyUploadRequest = PrekeyUpload


class PrekeyCountOut(_Schema):
    """Response for GET /devices/{device_id}/prekeys/count (and the replenish endpoint)."""

    device_id: uuid.UUID
    one_time_prekeys_available: int
    kyber_one_time_prekeys_available: int
    kyber_last_resort_registered: bool


# ---- prekey bundle fetch (GET /users/{user_id}/prekey-bundle) ----


class SignedPrekeyOut(_Schema):
    key_id: int
    public_key_b64: Base64Str = Field(
        validation_alias=AliasChoices("public_key_b64", "public_key")
    )
    signature_b64: Base64Str = Field(
        validation_alias=AliasChoices("signature_b64", "signature")
    )


class OneTimePrekeyOut(_Schema):
    key_id: int
    public_key_b64: Base64Str = Field(
        validation_alias=AliasChoices("public_key_b64", "public_key")
    )


class KyberPrekeyOut(_Schema):
    key_id: int
    public_key_b64: Base64Str = Field(
        validation_alias=AliasChoices("public_key_b64", "public_key")
    )
    signature_b64: Base64Str = Field(
        validation_alias=AliasChoices("signature_b64", "signature")
    )
    is_last_resort: bool


class DevicePrekeyBundleOut(_Schema):
    """One recipient device's bundle; one-time prekey is omitted when the pool is empty.

    `kyber_prekey` prefers a one-time Kyber prekey (consumed atomically like the EC OTK)
    and falls back to the device's last-resort Kyber prekey WITHOUT consuming it when the
    one-time pool is empty. It is null when the device never registered any Kyber prekey
    (pre-PQXDH registration) — nullable path for backward compatibility.
    """

    device_id: uuid.UUID
    registration_id: int
    identity_key_b64: Base64Str = Field(
        validation_alias=AliasChoices("identity_key_b64", "identity_key")
    )
    signed_prekey: SignedPrekeyOut
    one_time_prekey: OneTimePrekeyOut | None = None
    kyber_prekey: KyberPrekeyOut | None = None


class PrekeyBundleOut(_Schema):
    """Full bundle for a user: one entry per non-revoked device."""

    user_id: uuid.UUID
    devices: list[DevicePrekeyBundleOut]
