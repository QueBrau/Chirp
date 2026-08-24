"""Key directory router: device registration, prekey replenishment, prekey bundle fetch."""
import base64
import binascii
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found, too_many_requests
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.e2ee import (
    DeviceCreate,
    DeviceOut,
    DevicePrekeyBundleOut,
    KyberPrekeyOut,
    OneTimePrekeyOut,
    PrekeyBundleOut,
    PrekeyCountOut,
    PrekeyUpload,
    SignedPrekeyOut,
)
from app.services.prekey_service import (
    consume_one_time_kyber_prekey,
    consume_one_time_prekey,
    get_last_resort_kyber_prekey,
)
from app.services.rate_limit import allow as rate_limit_allow

router = APIRouter(tags=["keys"])

# SECURITY-REVIEW finding 9: prekey-bundle fetch consumes a one-time prekey per call, so an
# unthrottled caller can drain a victim's OTK pool without ever starting a session. Cap it per
# (caller, target) pair. See app.services.rate_limit for the per-instance caveat — this is a
# first-layer mitigation, not a hard guarantee (production should move to a Redis-backed
# counter using the client already in app.ws.pubsub.get_redis()).
_PREKEY_BUNDLE_RATE_LIMIT_MAX_CALLS = 10
_PREKEY_BUNDLE_RATE_LIMIT_WINDOW_SECONDS = 600.0  # 10 minutes


def _b64_to_bytes(value: str) -> bytes:
    """Decode a base64 body field, raising 422 on malformed input."""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="invalid_base64") from None


async def _get_owned_device(
    session: AsyncSession, device_id: uuid.UUID, user: models.User
) -> models.Device:
    """Load a device, 404 if missing, 403 unless owned by the caller."""
    device = await session.get(models.Device, device_id)
    if device is None:
        raise not_found("device_not_found")
    if device.user_id != user.id:
        raise forbidden("not_device_owner")
    return device


async def _available_otk_count(session: AsyncSession, device_id: uuid.UUID) -> int:
    """Count one-time prekeys with consumed_at IS NULL for a device."""
    result = await session.execute(
        select(func.count())
        .select_from(models.OneTimePrekey)
        .where(
            models.OneTimePrekey.device_id == device_id,
            models.OneTimePrekey.consumed_at.is_(None),
        )
    )
    return int(result.scalar_one())


async def _available_kyber_otk_count(session: AsyncSession, device_id: uuid.UUID) -> int:
    """Count one-time Kyber prekeys with consumed_at IS NULL for a device."""
    result = await session.execute(
        select(func.count())
        .select_from(models.KyberPrekey)
        .where(
            models.KyberPrekey.device_id == device_id,
            models.KyberPrekey.consumed_at.is_(None),
            models.KyberPrekey.is_last_resort.is_(False),
        )
    )
    return int(result.scalar_one())


async def _has_last_resort_kyber(session: AsyncSession, device_id: uuid.UUID) -> bool:
    """Whether the device has a last-resort Kyber prekey registered."""
    result = await session.execute(
        select(func.count())
        .select_from(models.KyberPrekey)
        .where(
            models.KyberPrekey.device_id == device_id,
            models.KyberPrekey.is_last_resort.is_(True),
        )
    )
    return int(result.scalar_one()) > 0


async def _prekey_count_out(session: AsyncSession, device_id: uuid.UUID) -> PrekeyCountOut:
    """Assemble the full PrekeyCountOut (EC + Kyber) for a device."""
    return PrekeyCountOut(
        device_id=device_id,
        one_time_prekeys_available=await _available_otk_count(session, device_id),
        kyber_one_time_prekeys_available=await _available_kyber_otk_count(session, device_id),
        kyber_last_resort_registered=await _has_last_resort_kyber(session, device_id),
    )


@router.post("/devices", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DeviceOut:
    """Register a device: identity key + signed prekey + a batch of one-time prekeys."""
    identity_key = _b64_to_bytes(body.identity_key_b64)
    signed_public = _b64_to_bytes(body.signed_prekey.public_key_b64)
    signed_signature = _b64_to_bytes(body.signed_prekey.signature_b64)
    otk_rows = [
        (otk.key_id, _b64_to_bytes(otk.public_key_b64)) for otk in body.one_time_prekeys
    ]
    kyber_otk_rows = [
        (kyber.key_id, _b64_to_bytes(kyber.public_key_b64), _b64_to_bytes(kyber.signature_b64))
        for kyber in body.kyber_one_time
    ]

    device = models.Device(
        user_id=user.id,
        device_label=body.device_label,
        registration_id=body.registration_id,
        identity_key=identity_key,
    )
    session.add(device)
    await session.flush()
    await session.refresh(device)

    session.add(
        models.SignedPrekey(
            device_id=device.id,
            key_id=body.signed_prekey.key_id,
            public_key=signed_public,
            signature=signed_signature,
        )
    )
    session.add_all(
        models.OneTimePrekey(device_id=device.id, key_id=key_id, public_key=public_key)
        for key_id, public_key in otk_rows
    )
    if body.kyber_last_resort is not None:
        session.add(
            models.KyberPrekey(
                device_id=device.id,
                key_id=body.kyber_last_resort.key_id,
                public_key=_b64_to_bytes(body.kyber_last_resort.public_key_b64),
                signature=_b64_to_bytes(body.kyber_last_resort.signature_b64),
                is_last_resort=True,
            )
        )
    session.add_all(
        models.KyberPrekey(
            device_id=device.id,
            key_id=key_id,
            public_key=public_key,
            signature=signature,
            is_last_resort=False,
        )
        for key_id, public_key, signature in kyber_otk_rows
    )
    await session.commit()
    return DeviceOut.model_validate(device)


@router.post("/devices/{device_id}/prekeys", response_model=PrekeyCountOut)
async def replenish_prekeys(
    device_id: uuid.UUID,
    body: PrekeyUpload,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PrekeyCountOut:
    """Replenish one-time prekeys (and optionally rotate the signed prekey). Owner only."""
    device = await _get_owned_device(session, device_id, user)
    if device.revoked_at is not None:
        raise forbidden("device_revoked")

    if body.signed_prekey is not None:
        session.add(
            models.SignedPrekey(
                device_id=device.id,
                key_id=body.signed_prekey.key_id,
                public_key=_b64_to_bytes(body.signed_prekey.public_key_b64),
                signature=_b64_to_bytes(body.signed_prekey.signature_b64),
            )
        )
    session.add_all(
        models.OneTimePrekey(
            device_id=device.id,
            key_id=otk.key_id,
            public_key=_b64_to_bytes(otk.public_key_b64),
        )
        for otk in body.one_time_prekeys
    )
    if body.kyber_last_resort is not None:
        # Rotation: insert a fresh row. The previous last-resort row is superseded (never
        # consumed, never deleted) — the bundle endpoint always selects the newest one.
        session.add(
            models.KyberPrekey(
                device_id=device.id,
                key_id=body.kyber_last_resort.key_id,
                public_key=_b64_to_bytes(body.kyber_last_resort.public_key_b64),
                signature=_b64_to_bytes(body.kyber_last_resort.signature_b64),
                is_last_resort=True,
            )
        )
    session.add_all(
        models.KyberPrekey(
            device_id=device.id,
            key_id=kyber.key_id,
            public_key=_b64_to_bytes(kyber.public_key_b64),
            signature=_b64_to_bytes(kyber.signature_b64),
            is_last_resort=False,
        )
        for kyber in body.kyber_one_time
    )
    await session.commit()

    return await _prekey_count_out(session, device.id)


@router.get("/devices/{device_id}/prekeys/count", response_model=PrekeyCountOut)
async def prekey_count(
    device_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PrekeyCountOut:
    """Report how many unconsumed prekeys (EC + Kyber) remain for a device. Owner only."""
    device = await _get_owned_device(session, device_id, user)
    return await _prekey_count_out(session, device.id)


@router.get("/users/{user_id}/prekey-bundle", response_model=PrekeyBundleOut)
async def fetch_prekey_bundle(
    user_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PrekeyBundleOut:
    """Return a bundle per non-revoked device, consuming one one-time prekey per device.

    The one-time prekey is null for a device whose pool is exhausted — the bundle is
    still returned (X3DH degrades gracefully without an OTK).

    Rate-limited per (caller, target) pair — see the module-level constants above — as a
    first-layer mitigation against pool-draining (SECURITY-REVIEW finding 9). Does not change
    OTK consumption semantics below; it only gates whether this call is allowed to happen.
    """
    rate_limit_key = f"prekey_bundle:{user.id}:{user_id}"
    if not await rate_limit_allow(
        rate_limit_key,
        max_calls=_PREKEY_BUNDLE_RATE_LIMIT_MAX_CALLS,
        window_seconds=_PREKEY_BUNDLE_RATE_LIMIT_WINDOW_SECONDS,
    ):
        raise too_many_requests("prekey_bundle_rate_limited")

    target = await session.get(models.User, user_id)
    if target is None:
        raise not_found("user_not_found")

    devices_result = await session.execute(
        select(models.Device)
        .where(models.Device.user_id == user_id, models.Device.revoked_at.is_(None))
        .order_by(models.Device.created_at)
    )
    bundles: list[DevicePrekeyBundleOut] = []
    for device in devices_result.scalars().all():
        spk_result = await session.execute(
            select(models.SignedPrekey)
            .where(models.SignedPrekey.device_id == device.id)
            .order_by(models.SignedPrekey.created_at.desc())
            .limit(1)
        )
        signed_prekey = spk_result.scalars().first()
        if signed_prekey is None:
            # Device has no usable bundle yet; skip it.
            continue
        otk = await consume_one_time_prekey(session, device.id)

        # Kyber: prefer a one-time Kyber prekey (consumed atomically, same as the EC OTK
        # above); fall back to the device's last-resort Kyber prekey WITHOUT consuming it
        # when the one-time pool is empty. Null when the device never registered any Kyber
        # prekey at all (nullable path — backward compatible with pre-PQXDH registrations).
        kyber = await consume_one_time_kyber_prekey(session, device.id)
        if kyber is None:
            kyber = await get_last_resort_kyber_prekey(session, device.id)

        bundles.append(
            DevicePrekeyBundleOut(
                device_id=device.id,
                registration_id=device.registration_id,
                identity_key_b64=device.identity_key,
                signed_prekey=SignedPrekeyOut.model_validate(signed_prekey),
                one_time_prekey=(
                    OneTimePrekeyOut.model_validate(otk) if otk is not None else None
                ),
                kyber_prekey=(
                    KyberPrekeyOut.model_validate(kyber) if kyber is not None else None
                ),
            )
        )
    # Persist OTK consumption atomically with the response.
    await session.commit()
    return PrekeyBundleOut(user_id=user_id, devices=bundles)
