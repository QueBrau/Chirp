"""Transaction-scoped write-hold primitive for account deletion.

This module deliberately has no HTTP/WS integration and never commits. Callers must
acquire the gate before their first write and commit or roll back the surrounding
transaction themselves.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

# These are the deletion states accepted by the existing operator preflight. The
# schema has a broader status union, but ``verifying`` has no current executor path.
ACTIVE_STATUSES = frozenset({"received", "processing", "blocked", "partially_completed"})


class DeletionHoldError(RuntimeError):
    """Base for fail-closed hold protocol errors."""


class DeletionHoldActive(DeletionHoldError):
    pass


class DeletionHoldInvalid(DeletionHoldError):
    pass


@dataclass(frozen=True)
class DeletionHoldView:
    id: uuid.UUID
    user_id: uuid.UUID
    request_id: uuid.UUID
    state: str
    activated_at: datetime


def _uid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        result = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DeletionHoldInvalid("invalid_identity") from None
    if str(value) != str(result):
        raise DeletionHoldInvalid("invalid_identity")
    return result


def advisory_key(user_id: uuid.UUID | str) -> int:
    """Return a stable signed bigint; hash collisions can only over-block users."""
    uid = _uid(user_id)
    return int.from_bytes(hashlib.sha256(uid.bytes).digest()[:8], "big", signed=True)


async def _protocol_start(session: AsyncSession, user_id: uuid.UUID) -> None:
    if session.new or session.dirty or session.deleted:
        raise DeletionHoldInvalid("hold_gate_must_be_first_operation")
    isolation = (await session.execute(text("SELECT current_setting('transaction_isolation')"))).scalar_one()
    if isolation != "read committed":
        raise DeletionHoldInvalid("read_committed_required")
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": advisory_key(user_id)}
    )
    await session.execute(
        text("SELECT set_config('chirp.deletion_hold_shared_key', :key, true)"),
        {"key": str(advisory_key(user_id))},
    )


async def _lock_exclusive(session: AsyncSession, user_id: uuid.UUID) -> None:
    if session.new or session.dirty or session.deleted:
        raise DeletionHoldInvalid("hold_gate_must_be_first_operation")
    isolation = (await session.execute(text("SELECT current_setting('transaction_isolation')"))).scalar_one()
    if isolation != "read committed":
        raise DeletionHoldInvalid("read_committed_required")
    shared_key = await session.execute(
        text("SELECT current_setting('chirp.deletion_hold_shared_key', true)")
    )
    if shared_key.scalar_one_or_none() not in (None, ""):
        # Advisory locks cannot be safely upgraded: another transaction may be
        # waiting for the exclusive lock while this transaction keeps sharing it.
        raise DeletionHoldInvalid("cannot_upgrade_shared_hold")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key(user_id)}
    )


def _view(row: models.AccountDeletionHold) -> DeletionHoldView:
    return DeletionHoldView(row.id, row.user_id, row.request_id, row.state, row.activated_at)


async def admit_user_write(session: AsyncSession, user_id: uuid.UUID | str) -> None:
    """Hold the user's shared gate and fail if a fresh active hold exists.

    The shared lock remains held until the caller's transaction ends. This function
    intentionally does not commit, flush, or accept a preloaded ORM user object.
    """
    uid = _uid(user_id)
    await _protocol_start(session, uid)
    row = (await session.execute(
        select(models.AccountDeletionHold.id)
        .where(models.AccountDeletionHold.user_id == uid, models.AccountDeletionHold.state == "active")
    )).first()
    if row is not None:
        raise DeletionHoldActive("account_deletion_hold_active")


async def activate_deletion_hold(
    session: AsyncSession, *, user_id: uuid.UUID | str, request_id: uuid.UUID | str,
) -> DeletionHoldView:
    """Create or return the active hold for this exact deletion request.

    The returned row is only provisional until the caller commits. No durable claim
    is made by this function itself.
    """
    uid, rid = _uid(user_id), _uid(request_id)
    await _lock_exclusive(session, uid)
    user = (await session.execute(
        select(models.User.id).where(models.User.id == uid).with_for_update()
    )).first()
    if user is None:
        raise DeletionHoldInvalid("user_not_found")
    request = (await session.execute(
        select(models.AccountDataRequest.id, models.AccountDataRequest.user_id,
               models.AccountDataRequest.kind, models.AccountDataRequest.status)
        .where(models.AccountDataRequest.id == rid).with_for_update()
    )).first()
    if request is None or request.user_id != uid or request.kind != "deletion" or request.status not in ACTIVE_STATUSES:
        raise DeletionHoldInvalid("deletion_request_invalid")
    existing = (await session.execute(
        select(models.AccountDeletionHold)
        .where(models.AccountDeletionHold.user_id == uid, models.AccountDeletionHold.state == "active")
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if existing is not None:
        if existing.request_id != rid:
            raise DeletionHoldActive("different_deletion_hold_active")
        return _view(existing)
    hold = models.AccountDeletionHold(user_id=uid, request_id=rid)
    session.add(hold)
    await session.flush()
    return _view(hold)


async def inspect_deletion_hold(
    session: AsyncSession, *, user_id: uuid.UUID | str, request_id: uuid.UUID | str | None = None,
) -> DeletionHoldView | None:
    """Read a fresh hold under the shared gate; does not attest quiescence."""
    uid = _uid(user_id)
    await _protocol_start(session, uid)
    stmt = select(models.AccountDeletionHold).where(
        models.AccountDeletionHold.user_id == uid, models.AccountDeletionHold.state == "active"
    ).execution_options(populate_existing=True)
    if request_id is not None:
        stmt = stmt.where(models.AccountDeletionHold.request_id == _uid(request_id))
    row = (await session.execute(stmt)).scalar_one_or_none()
    return _view(row) if row is not None else None
