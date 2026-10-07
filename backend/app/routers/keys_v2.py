"""E2EE v2 key directory router: register, approve, top up, directory read, claim, revoke (c444).

A VERSIONED API, NOT A REINTERPRETATION OF /devices (E2EE-DESIGN.md section 4). Everything
here lives under /v2, works only on rows with a non-NULL crypto_suite, and never touches the
legacy Signal-shaped tables' meaning. The v1 router is unchanged.

TRUST MODEL IN ONE PARAGRAPH. Registration is authenticated (Firebase) and every signature
is VERIFIED, not shape-checked (core/e2ee_v2.py defines the signed bytes). The first device of
an account is self-approved (a ROOT device: approved_at set, approved_by_device_id NULL). Any
later device is PENDING until a device that is already approved, unrevoked and owned by the
same user signs its identity (POST /v2/devices/{id}/approve). A pending device is invisible
to the directory, cannot be claimed, and cannot send or receive legs. So a stolen Firebase
token alone cannot add a readable device to an account that already has one: the approval
signature needs an approved device's private key. If an account has NO approved, unrevoked
device left (every phone lost or revoked), the next registration becomes a new root; that
is the "Reset security" path (design section 8), and every contact's client shows the
identity-change hard stop. The server cannot prevent that, only make it loud.

WHO MAY LOOK UP WHOM mirrors GET /users/{id}/prekey-bundle exactly: any authenticated
caller, the target user must exist (404 user_not_found), rate-limited per (caller, target).
That is an open lookup, as it is today. It is called out in the c444 report as a gap worth
closing with a shared-conversation check; it is deliberately not invented here.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.e2ee_v2 import (
    KEY_KIND_FALLBACK,
    KEY_KIND_ONE_TIME,
    SUPPORTED_SUITES,
    device_approval_message,
    device_binding_message,
    one_time_key_message,
    verify_ed25519,
)
from app.core.errors import conflict, forbidden, not_found, too_many_requests
from app.db import get_session
from app.middleware.auth import get_current_user
from app.core.rate_limits import limit_per_user
from app.routers.keys import (
    _DEVICE_REGISTER_RATE_MAX_CALLS,
    _DEVICE_REGISTER_RATE_WINDOW_SECONDS,
    _PREKEY_BUNDLE_RATE_LIMIT_MAX_CALLS,
    _PREKEY_BUNDLE_RATE_LIMIT_WINDOW_SECONDS,
    _PREKEY_WRITE_ACCOUNT_MAX_CALLS,
    _PREKEY_WRITE_DEVICE_MAX_CALLS,
    _PREKEY_WRITE_RATE_WINDOW_SECONDS,
    MAX_ACTIVE_DEVICES,
    _bounded_count,
    _check_device_quota,
    _get_owned_device,
)
from app.schemas.e2ee_v2 import (
    MAX_AVAILABLE_ONE_TIME_KEYS,
    ClaimedKeyOut,
    DeviceApproveV2,
    DeviceRegisterV2,
    DeviceV2Out,
    DirectoryDeviceOut,
    KeyClaimOut,
    KeyClaimRequest,
    KeyCountV2Out,
    KeyUploadV2,
    SignedKeyInput,
    UserDirectoryOut,
    b64,
)
from app.services.e2ee_v2_service import (
    approved_v2_device_conditions,
    approved_v2_devices_for_users,
    claim_one_time_key,
    conflict_response,
    get_fallback_key,
    lock_account,
)
from app.services.rate_limit import allow as rate_limit_allow

router = APIRouter(prefix="/v2", tags=["keys-v2"])

# Claim burns a one-time key per call, so it keeps v1's pool-drain guard (SECURITY-REVIEW
# finding 9): the same per-(caller, target) number as the v1 bundle fetch. The fallback key
# means an exhausted pool degrades forward secrecy of a first message, it does not stop
# delivery, which is why this is a rate limit and not a hard quota.
_CLAIM_TARGET_RATE_MAX_CALLS = _PREKEY_BUNDLE_RATE_LIMIT_MAX_CALLS
_CLAIM_TARGET_RATE_WINDOW_SECONDS = _PREKEY_BUNDLE_RATE_LIMIT_WINDOW_SECONDS
# Per-caller ceiling across ALL targets, so a caller cannot sweep many victims one low
# per-target budget at a time. Also bounds guessing of device ids.
CLAIM_CALLER_LIMIT = (60, 600)
# The directory read claims nothing, so it can be far looser than a claim. A client
# refreshes it on every send that finds its cache stale and on every device_list_mismatch.
_DIRECTORY_RATE_MAX_CALLS = 60
_DIRECTORY_RATE_WINDOW_SECONDS = 600.0
# Approving or revoking a phone is a rare, deliberate act.
DEVICE_ADMIN_LIMIT = (30, 3600)


def _decode(value: str) -> bytes:
    """Decode a field the schema has already proven is exact-width base64."""
    return base64.b64decode(value, validate=True)


def _device_out(device: models.Device) -> DeviceV2Out:
    """Owner's view of a v2 device row."""
    assert device.crypto_suite is not None and device.generation is not None
    assert device.identity_ed25519 is not None and device.binding_signature is not None
    return DeviceV2Out(
        id=device.id,
        user_id=device.user_id,
        device_label=device.device_label,
        suite=device.crypto_suite,
        generation=device.generation,
        identity_curve25519_b64=b64(device.identity_key) or "",
        identity_ed25519_b64=b64(device.identity_ed25519) or "",
        binding_signature_b64=b64(device.binding_signature) or "",
        approved=device.approved_at is not None,
        approved_at=device.approved_at,
        approved_by_device_id=device.approved_by_device_id,
        approval_signature_b64=b64(device.approval_signature),
        created_at=device.created_at,
        revoked_at=device.revoked_at,
    )


def _verified_key_rows(
    keys: list[SignedKeyInput],
    *,
    kind: str,
    identity_curve25519: bytes,
    identity_ed25519: bytes,
    detail: str,
) -> list[tuple[int, bytes, bytes]]:
    """Verify every key's signature against the device's Ed25519 identity.

    Returns (key_id, public_key, signature). One bad signature refuses the whole upload
    with the fixed `detail` code, before any row is staged: a batch that is partly
    accepted would leave the client unsure which keys the server will hand out.
    """
    rows: list[tuple[int, bytes, bytes]] = []
    for key in keys:
        public_key = _decode(key.public_key_b64)
        signature = _decode(key.signature_b64)
        message = one_time_key_message(
            kind=kind,
            key_id=key.key_id,
            public_key=public_key,
            identity_curve25519=identity_curve25519,
        )
        if not verify_ed25519(identity_ed25519, signature, message):
            raise HTTPException(status_code=422, detail=detail)
        rows.append((key.key_id, public_key, signature))
    return rows


def _refuse_duplicate_key_ids(*groups: list[SignedKeyInput | None]) -> None:
    """A key id is never reused within a device: refuse repeats inside one request."""
    ids = [key.key_id for group in groups for key in group if key is not None]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="duplicate_key_id")


async def _max_generation(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Highest generation over ALL of the user's device rows (revoked and pending too)."""
    value = await session.scalar(
        select(func.max(models.Device.generation)).where(models.Device.user_id == user_id)
    )
    return int(value or 0)


def _constraint_in(exc: IntegrityError, name: str) -> bool:
    return name in str(getattr(exc, "orig", exc))


@router.post(
    "/devices",
    response_model=DeviceV2Out,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "stale_generation (body carries max_generation), "
                     "identity_already_registered, device caps"}},
)
async def register_device_v2(
    body: DeviceRegisterV2,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DeviceV2Out | JSONResponse:
    """Register a v2 device. Every signature is verified; the first device is self-approved.

    Refusals before any row is written, in this order: rate limit, unsupported suite,
    binding signature, each key signature, duplicate key ids, device caps, generation.
    The user row stays locked from the cap check to the commit, so two simultaneous
    registrations on one account cannot both read the same max generation or both
    decide they are the root.
    """
    if not await rate_limit_allow(
        f"device_register:{user.id}",
        max_calls=_DEVICE_REGISTER_RATE_MAX_CALLS,
        window_seconds=_DEVICE_REGISTER_RATE_WINDOW_SECONDS,
    ):
        raise too_many_requests("device_registration_rate_limited")
    if body.suite not in SUPPORTED_SUITES:
        raise HTTPException(status_code=422, detail="unsupported_suite")
    # Captured now: the IntegrityError path below rolls the session back, which expires
    # every ORM instance on it (including `user`), and reading an expired attribute from
    # async code raises MissingGreenlet instead of reloading.
    user_id = user.id

    curve = _decode(body.identity_curve25519_b64)
    ed = _decode(body.identity_ed25519_b64)
    binding_signature = _decode(body.binding_signature_b64)
    binding = device_binding_message(
        suite=body.suite,
        user_id=user_id,
        generation=body.generation,
        identity_curve25519=curve,
        identity_ed25519=ed,
    )
    if not verify_ed25519(ed, binding_signature, binding):
        raise HTTPException(status_code=422, detail="invalid_binding_signature")
    one_time_rows = _verified_key_rows(
        body.one_time_keys,
        kind=KEY_KIND_ONE_TIME,
        identity_curve25519=curve,
        identity_ed25519=ed,
        detail="invalid_one_time_key_signature",
    )
    (fallback_row,) = _verified_key_rows(
        [body.fallback_key],
        kind=KEY_KIND_FALLBACK,
        identity_curve25519=curve,
        identity_ed25519=ed,
        detail="invalid_fallback_key_signature",
    )
    _refuse_duplicate_key_ids(list(body.one_time_keys), [body.fallback_key])

    # Locks the user row; held until commit or rollback.
    await _check_device_quota(session, user_id)
    max_generation = await _max_generation(session, user_id)
    if body.generation <= max_generation:
        return conflict_response("stale_generation", max_generation=max_generation)

    has_approved_device = await session.scalar(
        select(models.Device.id)
        .where(models.Device.user_id == user_id, *approved_v2_device_conditions())
        .limit(1)
    )
    # ROOT: no approved, unrevoked v2 device exists, so there is nobody to approve this
    # one. It is self-approved (approved_by_device_id stays NULL) and trust rests on the
    # contacts' first-use pin plus the identity-change hard stop.
    is_root = has_approved_device is None

    device = models.Device(
        user_id=user_id,
        device_label=body.device_label,
        registration_id=None,
        identity_key=curve,
        crypto_suite=body.suite,
        identity_ed25519=ed,
        generation=body.generation,
        binding_signature=binding_signature,
        approved_at=datetime.now(timezone.utc) if is_root else None,
    )
    session.add(device)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if _constraint_in(exc, "uq_devices_v2_user_generation"):
            return conflict_response(
                "stale_generation", max_generation=await _max_generation(session, user_id)
            )
        if _constraint_in(exc, "uq_devices_v2_identity"):
            raise conflict("identity_already_registered") from None
        raise
    await session.refresh(device)

    session.add_all(
        models.OneTimePrekey(
            device_id=device.id, key_id=key_id, public_key=public_key,
            signature=signature, kind=KEY_KIND_ONE_TIME,
        )
        for key_id, public_key, signature in one_time_rows
    )
    session.add(
        models.OneTimePrekey(
            device_id=device.id, key_id=fallback_row[0], public_key=fallback_row[1],
            signature=fallback_row[2], kind=KEY_KIND_FALLBACK,
        )
    )
    await session.commit()
    return _device_out(device)


@router.post(
    "/devices/{device_id}/approve",
    response_model=DeviceV2Out,
    dependencies=[Depends(limit_per_user("e2ee_device_admin", DEVICE_ADMIN_LIMIT))],
)
async def approve_device_v2(
    device_id: uuid.UUID,
    body: DeviceApproveV2,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DeviceV2Out:
    """Approve a pending device with a signature from an approved device of the same user.

    The approver must be v2, approved, unrevoked and owned by the CALLER; anything else
    is the one fixed `invalid_approver` refusal (a distinct code per cause would let a
    caller probe other accounts' device ids). The signature is checked over the NEW
    device's (user_id, generation, both identities) with the APPROVER's Ed25519 key.
    """
    await lock_account(session, user.id)
    target = await _get_owned_device(session, device_id, user, lock=True)
    if target.crypto_suite is None:
        raise forbidden("device_suite_unsupported")
    if target.revoked_at is not None:
        raise forbidden("device_revoked")

    approver = await session.scalar(
        select(models.Device)
        .where(models.Device.id == body.approver_device_id)
        # Shared lock: a concurrent revoke of the approver (exclusive) waits for this
        # commit instead of racing it.
        .with_for_update(read=True)
    )
    if (
        approver is None
        or approver.user_id != user.id
        or approver.id == target.id
        or approver.crypto_suite is None
        or approver.approved_at is None
        or approver.revoked_at is not None
        or approver.identity_ed25519 is None
    ):
        raise forbidden("invalid_approver")

    assert target.generation is not None and target.identity_ed25519 is not None
    message = device_approval_message(
        user_id=user.id,
        generation=target.generation,
        identity_curve25519=target.identity_key,
        identity_ed25519=target.identity_ed25519,
    )
    signature = _decode(body.approval_signature_b64)
    if not verify_ed25519(approver.identity_ed25519, signature, message):
        raise HTTPException(status_code=422, detail="invalid_approval_signature")

    if target.approved_at is not None:
        # Idempotent retry of the SAME approval (a lost response) succeeds; anything
        # else cannot rewrite an approval that already exists.
        if target.approved_by_device_id == approver.id and target.approval_signature == signature:
            return _device_out(target)
        raise conflict("device_already_approved")

    target.approved_at = datetime.now(timezone.utc)
    target.approved_by_device_id = approver.id
    target.approval_signature = signature
    await session.commit()
    return _device_out(target)


async def _available_counts(session: AsyncSession, device_id: uuid.UUID) -> KeyCountV2Out:
    one_time = await _bounded_count(
        session,
        select(models.OneTimePrekey.id).where(
            models.OneTimePrekey.device_id == device_id,
            models.OneTimePrekey.kind == KEY_KIND_ONE_TIME,
            models.OneTimePrekey.consumed_at.is_(None),
        ),
        limit=MAX_AVAILABLE_ONE_TIME_KEYS,
    )
    fallback = await get_fallback_key(session, device_id)
    return KeyCountV2Out(
        device_id=device_id,
        one_time_keys_available=one_time,
        fallback_keys_available=0 if fallback is None else 1,
    )


@router.post("/devices/{device_id}/keys", response_model=KeyCountV2Out)
async def upload_keys_v2(
    device_id: uuid.UUID,
    body: KeyUploadV2,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KeyCountV2Out:
    """Top up one-time keys and/or replace the fallback key. Owner only.

    Replacing the fallback DELETES the old row and inserts the new one in one transaction
    (at most one fallback per device is a database invariant). The unconsumed one-time pool
    is capped so a device cannot park an unbounded stock of claimable keys; consumed rows
    are retired by the existing key-retirement job in app.jobs.purge, which already
    deletes any consumed one_time_prekeys row past the grace window.
    """
    if not await rate_limit_allow(
        f"e2ee_key_write_account:{user.id}",
        max_calls=_PREKEY_WRITE_ACCOUNT_MAX_CALLS,
        window_seconds=_PREKEY_WRITE_RATE_WINDOW_SECONDS,
    ) or not await rate_limit_allow(
        f"e2ee_key_write_device:{user.id}:{device_id}",
        max_calls=_PREKEY_WRITE_DEVICE_MAX_CALLS,
        window_seconds=_PREKEY_WRITE_RATE_WINDOW_SECONDS,
    ):
        raise too_many_requests("prekey_write_rate_limited")
    if not body.one_time_keys and body.fallback_key is None:
        raise HTTPException(status_code=422, detail="no_keys_uploaded")

    device = await _get_owned_device(session, device_id, user, lock=True)
    if device.revoked_at is not None:
        raise forbidden("device_revoked")
    if device.crypto_suite is None or device.identity_ed25519 is None:
        raise forbidden("device_suite_unsupported")

    one_time_rows = _verified_key_rows(
        body.one_time_keys,
        kind=KEY_KIND_ONE_TIME,
        identity_curve25519=device.identity_key,
        identity_ed25519=device.identity_ed25519,
        detail="invalid_one_time_key_signature",
    )
    fallback_rows = _verified_key_rows(
        [body.fallback_key] if body.fallback_key is not None else [],
        kind=KEY_KIND_FALLBACK,
        identity_curve25519=device.identity_key,
        identity_ed25519=device.identity_ed25519,
        detail="invalid_fallback_key_signature",
    )
    _refuse_duplicate_key_ids(list(body.one_time_keys), [body.fallback_key])

    if one_time_rows:
        counts = await _available_counts(session, device.id)
        if counts.one_time_keys_available + len(one_time_rows) > MAX_AVAILABLE_ONE_TIME_KEYS:
            raise conflict("one_time_key_pool_full")

    if fallback_rows:
        await session.execute(
            sql_delete(models.OneTimePrekey).where(
                models.OneTimePrekey.device_id == device.id,
                models.OneTimePrekey.kind == KEY_KIND_FALLBACK,
            )
        )
    session.add_all(
        models.OneTimePrekey(
            device_id=device.id, key_id=key_id, public_key=public_key,
            signature=signature, kind=KEY_KIND_ONE_TIME,
        )
        for key_id, public_key, signature in one_time_rows
    )
    session.add_all(
        models.OneTimePrekey(
            device_id=device.id, key_id=key_id, public_key=public_key,
            signature=signature, kind=KEY_KIND_FALLBACK,
        )
        for key_id, public_key, signature in fallback_rows
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _constraint_in(exc, "uq_otk_v2_device_key_id"):
            raise conflict("key_id_reused") from None
        raise
    return await _available_counts(session, device.id)


@router.get("/devices/{device_id}/keys/count", response_model=KeyCountV2Out)
async def key_count_v2(
    device_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KeyCountV2Out:
    """Unconsumed keys per kind, for the client's refill threshold. Owner only."""
    device = await _get_owned_device(session, device_id, user)
    if device.crypto_suite is None:
        raise forbidden("device_suite_unsupported")
    return await _available_counts(session, device.id)


@router.get("/users/{user_id}/devices", response_model=UserDirectoryOut)
async def get_user_directory(
    user_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserDirectoryOut:
    """The user's approved, unrevoked v2 devices. CLAIMS NOTHING.

    This is the fix for the contract finding that v1's bundle fetch burns a one-time key
    before anything is sent: reading identities and approval chains here is free and
    repeatable, and key consumption is a separate, explicit POST /v2/keys/claim. Pending,
    revoked and legacy devices are never listed.
    """
    if not await rate_limit_allow(
        f"e2ee_directory:{user.id}:{user_id}",
        max_calls=_DIRECTORY_RATE_MAX_CALLS,
        window_seconds=_DIRECTORY_RATE_WINDOW_SECONDS,
    ):
        raise too_many_requests("e2ee_directory_rate_limited")
    if await session.get(models.User, user_id) is None:
        raise not_found("user_not_found")

    devices = await approved_v2_devices_for_users(session, [user_id], limit=MAX_ACTIVE_DEVICES)
    if len(devices) > MAX_ACTIVE_DEVICES:
        # Never silently omit a supported device.
        raise conflict("active_device_limit_reached")

    approver_ids = {d.approved_by_device_id for d in devices if d.approved_by_device_id}
    approver_keys: dict[uuid.UUID, bytes | None] = {}
    if approver_ids:
        rows = await session.execute(
            select(models.Device.id, models.Device.identity_ed25519).where(
                models.Device.id.in_(approver_ids)
            )
        )
        approver_keys = {row.id: row.identity_ed25519 for row in rows}

    entries = []
    for device in devices:
        assert device.crypto_suite is not None and device.generation is not None
        assert device.identity_ed25519 is not None and device.binding_signature is not None
        entries.append(
            DirectoryDeviceOut(
                device_id=device.id,
                suite=device.crypto_suite,
                generation=device.generation,
                identity_curve25519_b64=b64(device.identity_key) or "",
                identity_ed25519_b64=b64(device.identity_ed25519) or "",
                binding_signature_b64=b64(device.binding_signature) or "",
                created_at=device.created_at,
                approved_by_device_id=device.approved_by_device_id,
                approval_signature_b64=b64(device.approval_signature),
                approver_identity_ed25519_b64=b64(
                    approver_keys.get(device.approved_by_device_id)
                    if device.approved_by_device_id is not None
                    else None
                ),
            )
        )
    return UserDirectoryOut(user_id=user_id, devices=entries)


@router.post(
    "/keys/claim",
    response_model=KeyClaimOut,
    dependencies=[Depends(limit_per_user("e2ee_claim", CLAIM_CALLER_LIMIT))],
    responses={409: {"description": "device_unavailable (body carries device_ids); nothing claimed"}},
)
async def claim_keys(
    body: KeyClaimRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KeyClaimOut | JSONResponse:
    """Atomically claim one key per named device: a one-time key, else the fallback key.

    ALL OR NOTHING. If any named device is not currently claimable (unknown, pending,
    revoked or legacy) the answer is 409 `device_unavailable` listing those ids and NO key
    is consumed: the client refreshes its directory and retries with the right set.
    Claiming the others and reporting the rest later would burn keys for a send that is
    about to be redone.

    A one-time key is consumed (UPDATE ... SKIP LOCKED, so concurrent claimers never
    share one); a fallback key is returned WITHOUT being consumed. Rows are staged in one
    transaction and committed once at the end, so a failure part-way through releases every
    key already taken.
    """
    ids = body.device_ids
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="duplicate_device_ids")

    rows = await session.execute(
        select(models.Device).where(
            models.Device.id.in_(ids), *approved_v2_device_conditions()
        )
    )
    devices = {device.id: device for device in rows.scalars()}
    unavailable = [str(device_id) for device_id in ids if device_id not in devices]
    if unavailable:
        return conflict_response("device_unavailable", device_ids=unavailable)

    for target_user_id in sorted({device.user_id for device in devices.values()}, key=str):
        if not await rate_limit_allow(
            f"e2ee_claim_target:{user.id}:{target_user_id}",
            max_calls=_CLAIM_TARGET_RATE_MAX_CALLS,
            window_seconds=_CLAIM_TARGET_RATE_WINDOW_SECONDS,
        ):
            raise too_many_requests("e2ee_claim_rate_limited")

    claimed: list[ClaimedKeyOut] = []
    for device_id in ids:
        key = await claim_one_time_key(session, device_id)
        if key is None:
            key = await get_fallback_key(session, device_id)
        if key is None or key.signature is None:
            # A device with neither (should be impossible: registration requires a
            # fallback key). Roll back every key taken so far.
            return conflict_response("device_unavailable", device_ids=[str(device_id)])
        claimed.append(
            ClaimedKeyOut(
                device_id=device_id,
                key_id=key.key_id,
                kind=key.kind,  # type: ignore[arg-type]  # CHECK-constrained to the two literals
                public_key_b64=b64(key.public_key) or "",
                signature_b64=b64(key.signature) or "",
            )
        )
    await session.commit()
    return KeyClaimOut(keys=claimed)


@router.delete(
    "/devices/{device_id}",
    response_model=DeviceV2Out,
    dependencies=[Depends(limit_per_user("e2ee_device_admin", DEVICE_ADMIN_LIMIT))],
)
async def revoke_device_v2(
    device_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DeviceV2Out:
    """Revoke one of the caller's v2 devices. Idempotent: a second call changes nothing.

    Existing revocation semantics: set revoked_at and nothing else. The row is never
    deleted (it stays the audit trail and keeps counting toward MAX_RETAINED_DEVICES), and
    its key rows are retired later by the purge job's key-retirement phase. A revoked
    device vanishes from the directory, cannot be claimed, and cannot send or receive
    legs. Devices it approved stay approved: an approval is a historical fact, and the
    server cannot know which of them the user still trusts.

    Takes the same per-account lock as register and approve, so revoking the last
    approved device cannot interleave with a registration deciding whether the account
    already has an approver.
    """
    await lock_account(session, user.id)
    device = await _get_owned_device(session, device_id, user, lock=True)
    if device.crypto_suite is None:
        raise forbidden("device_suite_unsupported")
    if device.revoked_at is None:
        device.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    return _device_out(device)
