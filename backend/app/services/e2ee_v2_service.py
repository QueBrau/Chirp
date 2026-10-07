"""Shared E2EE v2 directory queries and the atomic key claim (board card c444).

Two routers (routers/keys_v2.py and routers/messages_v2.py) need the SAME definition of
"a device that can currently take part in v2", and a definition copied into two files is
how a device ends up claimable but not sendable-to (or the reverse). It lives here once:

    suite is not NULL            -- a legacy (Signal-shaped) row is refused by v2
    approved_at is not NULL      -- a pending device receives nothing and is never listed
    revoked_at is NULL           -- a revoked device is gone from every read and every send
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.e2ee_v2 import KEY_KIND_FALLBACK, KEY_KIND_ONE_TIME


def approved_v2_device_conditions() -> tuple[Any, ...]:
    """WHERE conditions for a device that is v2, approved and not revoked."""
    return (
        models.Device.crypto_suite.is_not(None),
        models.Device.approved_at.is_not(None),
        models.Device.revoked_at.is_(None),
    )


async def approved_v2_devices_for_users(
    session: AsyncSession, user_ids: Iterable[uuid.UUID], *, limit: int
) -> list[models.Device]:
    """Approved, unrevoked v2 devices of the given users, oldest first.

    `limit` is the caller's hard ceiling (members x MAX_ACTIVE_DEVICES). One extra row is
    fetched so a set above the ceiling is REFUSED by the caller rather than silently
    truncated: an omitted device would be a recipient that never gets a leg.
    """
    result = await session.execute(
        select(models.Device)
        .where(models.Device.user_id.in_(list(user_ids)), *approved_v2_device_conditions())
        .order_by(models.Device.created_at, models.Device.id)
        .limit(limit + 1)
    )
    return list(result.scalars().all())


async def lock_account(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize every device-state change for one account (register, approve, revoke).

    Same row lock routers.keys._check_device_quota already takes at registration. Taking
    it in approve and revoke too means "is there an approved device that could approve
    this one" cannot change between the check and the write.
    """
    await session.execute(
        select(models.User.id).where(models.User.id == user_id).with_for_update()
    )


async def claim_one_time_key(
    session: AsyncSession, device_id: uuid.UUID
) -> models.OneTimePrekey | None:
    """Atomically consume one unconsumed v2 one-time key for the device, or return None.

    UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING ...: two
    concurrent claimers can never be handed the same key, because the loser's subselect
    skips the row the winner has locked and takes the next one (or finds none, and the
    caller falls back). Fallback rows are excluded here by kind: they are never consumed.
    ORDER BY key_id makes the handout order deterministic (oldest key first).
    """
    candidate_id = (
        select(models.OneTimePrekey.id)
        .where(
            models.OneTimePrekey.device_id == device_id,
            models.OneTimePrekey.kind == KEY_KIND_ONE_TIME,
            # Signed rows only: v2 rows always carry a signature, and a row without one
            # is a legacy v1 key that must never be handed out as a v2 key.
            models.OneTimePrekey.signature.is_not(None),
            models.OneTimePrekey.consumed_at.is_(None),
        )
        .order_by(models.OneTimePrekey.key_id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = await session.execute(
        update(models.OneTimePrekey)
        .where(models.OneTimePrekey.id == candidate_id)
        .values(consumed_at=func.now())
        .returning(models.OneTimePrekey)
    )
    return result.scalars().first()


async def get_fallback_key(
    session: AsyncSession, device_id: uuid.UUID
) -> models.OneTimePrekey | None:
    """The device's current fallback key. Never marks it consumed.

    There is at most one row (uq_otk_v2_one_fallback_per_device); replacing the key
    deletes the old row in the same transaction as the insert.
    """
    result = await session.execute(
        select(models.OneTimePrekey).where(
            models.OneTimePrekey.device_id == device_id,
            models.OneTimePrekey.kind == KEY_KIND_FALLBACK,
        )
    )
    return result.scalars().first()


def conflict_response(detail: str, **body: Any) -> JSONResponse:
    """409 whose `detail` is the fixed machine-readable code AND which carries extra fields.

    core.errors.conflict raises an HTTPException, whose body is only {"detail": ...}.
    device_list_mismatch, stale_generation and device_unavailable have to hand the client
    data it needs to recover (the current device ids, the current generation), and nesting
    that under `detail` would break the convention that clients compare `detail` to a
    string. Handlers RETURN this instead of raising; returning rolls the session back on
    close, so nothing the handler had staged is committed.
    """
    return JSONResponse(status_code=409, content={"detail": detail, **body})
