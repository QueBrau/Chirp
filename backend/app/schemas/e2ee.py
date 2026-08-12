"""E2EE key-directory schemas: devices, signed/one-time prekeys, prekey bundles."""

import base64
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field


def _to_b64(value: object) -> object:
    """Encode raw bytes (from ORM BYTEA columns) to a base64 str; pass strings through."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


Base64Str = Annotated[str, BeforeValidator(_to_b64)]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- prekey inputs ----


class SignedPrekeyCreate(_Schema):
    key_id: int
    public_key_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


class OneTimePrekeyCreate(_Schema):
    key_id: int
    public_key_b64: str = Field(min_length=1)


# ---- device registration ----


class DeviceCreate(_Schema):
    """Body for POST /devices — identity key + signed prekey + one-time prekey batch."""

    device_label: str | None = None
    registration_id: int
    identity_key_b64: str = Field(min_length=1)
    signed_prekey: SignedPrekeyCreate
    one_time_prekeys: list[OneTimePrekeyCreate] = Field(default_factory=list)


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
    """Replenish one-time prekeys and/or rotate the signed prekey."""

    signed_prekey: SignedPrekeyCreate | None = None
    one_time_prekeys: list[OneTimePrekeyCreate] = Field(default_factory=list)


PrekeyUploadRequest = PrekeyUpload


class PrekeyCountOut(_Schema):
    """Response for GET /devices/{device_id}/prekeys/count."""

    device_id: uuid.UUID
    one_time_prekeys_available: int


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


class DevicePrekeyBundleOut(_Schema):
    """One recipient device's bundle; one-time prekey is omitted when the pool is empty."""

    device_id: uuid.UUID
    registration_id: int
    identity_key_b64: Base64Str = Field(
        validation_alias=AliasChoices("identity_key_b64", "identity_key")
    )
    signed_prekey: SignedPrekeyOut
    one_time_prekey: OneTimePrekeyOut | None = None


class PrekeyBundleOut(_Schema):
    """Full bundle for a user: one entry per non-revoked device."""

    user_id: uuid.UUID
    devices: list[DevicePrekeyBundleOut]
