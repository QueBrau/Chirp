"""Fenced, bounded registry for live WebSocket subscriptions."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app import models
from app.db import get_session_factory
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    _lock_exclusive,
)

MAX_WS_CONNECTIONS_PER_ACCOUNT = 32
WsConnectionState = Literal["finished", "uncertain"]


@dataclass(frozen=True)
class WsConnectionHandle:
    connection_id: uuid.UUID
    user_id: uuid.UUID
    slot: int
    owner_token: uuid.UUID = field(repr=False)


class WsConnectionLimit(RuntimeError):
    """All bounded slots are occupied, including unresolved connections."""


def _canonical_uuid(value: uuid.UUID | str, *, reason: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DeletionHoldInvalid(reason) from None
    if str(value) != str(parsed):
        raise DeletionHoldInvalid(reason)
    return parsed


async def register_ws_connection(
    *, user_id: uuid.UUID, firebase_uid: str,
) -> WsConnectionHandle:
    """Commit a live-connection slot before the caller accepts or opens Redis.

    The exclusive account gate serializes slot allocation with hold activation and
    executor drain. No DB connection is retained after this short transaction.
    """
    account_id = _canonical_uuid(user_id, reason="invalid_identity")
    if not isinstance(firebase_uid, str) or not firebase_uid or len(firebase_uid) > 256:
        raise DeletionHoldInvalid("invalid_firebase_identity")

    connection_id, owner_token = uuid.uuid4(), uuid.uuid4()
    async with get_session_factory()() as session:
        try:
            await _lock_exclusive(session, account_id)
            user = (await session.execute(
                select(models.User.firebase_uid, models.User.suspended_at)
                .where(models.User.id == account_id)
            )).one_or_none()
            if user is None:
                raise DeletionHoldInvalid("user_not_found")
            if user.firebase_uid != firebase_uid:
                raise DeletionHoldInvalid("write_guard_identity_changed")
            if user.firebase_uid == f"deleted:{account_id}":
                raise DeletionHoldInvalid("account_deleted")
            if user.suspended_at is not None:
                raise DeletionHoldInvalid("account_suspended")

            active_hold = await session.scalar(
                select(models.AccountDeletionHold.id).where(
                    models.AccountDeletionHold.user_id == account_id,
                    models.AccountDeletionHold.state == "active",
                )
            )
            if active_hold is not None:
                raise DeletionHoldActive("account_deletion_hold_active")

            occupied = set((await session.scalars(
                select(models.AccountWsConnection.slot).where(
                    models.AccountWsConnection.user_id == account_id
                )
            )).all())
            slot = next(
                (candidate for candidate in range(MAX_WS_CONNECTIONS_PER_ACCOUNT)
                 if candidate not in occupied),
                None,
            )
            if slot is None:
                raise WsConnectionLimit("websocket_connection_limit")

            session.add(models.AccountWsConnection(
                id=connection_id,
                user_id=account_id,
                slot=slot,
                owner_token=owner_token,
                state="running",
            ))
            await session.commit()
        except (DeletionHoldActive, DeletionHoldInvalid, WsConnectionLimit):
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise DeletionHoldInvalid("websocket_admission_unavailable") from None

    return WsConnectionHandle(
        connection_id=connection_id,
        user_id=account_id,
        slot=slot,
        owner_token=owner_token,
    )


async def finish_ws_connection(
    handle: WsConnectionHandle,
    *,
    state: WsConnectionState = "finished",
) -> None:
    """Release only a confirmed closed connection; unresolved slots stay blocking."""
    if not isinstance(handle, WsConnectionHandle):
        raise DeletionHoldInvalid("invalid_websocket_connection_handle")
    connection_id = _canonical_uuid(handle.connection_id, reason="invalid_websocket_connection_handle")
    account_id = _canonical_uuid(handle.user_id, reason="invalid_websocket_connection_handle")
    owner_token = _canonical_uuid(handle.owner_token, reason="invalid_websocket_connection_handle")
    if (
        isinstance(handle.slot, bool)
        or not isinstance(handle.slot, int)
        or not 0 <= handle.slot < MAX_WS_CONNECTIONS_PER_ACCOUNT
    ):
        raise DeletionHoldInvalid("invalid_websocket_connection_handle")
    if not isinstance(state, str) or state not in {"finished", "uncertain"}:
        raise DeletionHoldInvalid("invalid_websocket_connection_state")

    async with get_session_factory()() as session:
        try:
            row = (await session.execute(
                select(models.AccountWsConnection)
                .where(models.AccountWsConnection.id == connection_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            # Confirmed teardown can be retried after an ambiguous response. A
            # missing row cannot keep a live socket active; registration never
            # returns a handle until its insert commit succeeds.
            if row is None:
                await session.rollback()
                return
            if (
                row.user_id != account_id
                or row.slot != handle.slot
                or row.owner_token != owner_token
            ):
                raise DeletionHoldInvalid("websocket_connection_fence_invalid")
            if row.state == "uncertain":
                if state == "uncertain":
                    await session.rollback()
                    return
                raise DeletionHoldInvalid("websocket_connection_uncertain")
            if row.state != "running":
                raise DeletionHoldInvalid("websocket_connection_state_invalid")
            if state == "finished":
                await session.delete(row)
            else:
                row.state = "uncertain"
            await session.commit()
        except DeletionHoldInvalid:
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise DeletionHoldInvalid("websocket_admission_unavailable") from None
