"""Session-local account write admission for deletion holds.

The guard is deliberately attached to one SQLAlchemy session instance.  It does not
install a global event handler, commit transactions, or keep a database connection
alive while a caller performs external work.  The binding persists across commit and
rollback; each new transaction runs the guard again.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    advisory_key,
)

_INFO_KEY = "c436_account_write_guard"
_FAILED_KEY = "c436_account_write_guard_failed"
GUARD_LOCK_TIMEOUT_MS = 5_000


def _canonical_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        result = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DeletionHoldInvalid("invalid_identity") from None
    if str(value) != str(result):
        raise DeletionHoldInvalid("invalid_identity")
    return result


def _validate_uid(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise DeletionHoldInvalid("invalid_firebase_identity")
    return value


def _timeout_millis(value: str) -> float | None:
    if value == "0":
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h)", value)
    if match is None:
        raise DeletionHoldInvalid("invalid_lock_timeout")
    amount = float(match.group(1))
    factor = {"us": 0.001, "ms": 1.0, "s": 1000.0, "min": 60000.0, "h": 3600000.0}[match.group(2)]
    return amount * factor


def _failed(sync_session: Session) -> None:
    if sync_session.info.get(_FAILED_KEY):
        raise DeletionHoldInvalid("write_guard_transaction_failed_rollback_required")


def _before_flush(sync_session: Session, *_: Any) -> None:
    _failed(sync_session)


def _before_commit(sync_session: Session) -> None:
    _failed(sync_session)


def _before_orm_execute(orm_execute_state: Any) -> None:
    _failed(orm_execute_state.session)


def _after_soft_rollback(sync_session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:
        sync_session.info.pop(_FAILED_KEY, None)


def _after_begin(sync_session: Session, transaction: Any, connection: Any) -> None:
    """Install the shared gate and verify identity at each outer transaction start."""
    if transaction.parent is not None:
        return
    binding = sync_session.info.get(_INFO_KEY)
    try:
        if not isinstance(binding, tuple) or len(binding) != 2:
            raise DeletionHoldInvalid("invalid_write_guard_binding")
        user_id, firebase_uid = binding
        if not isinstance(user_id, uuid.UUID) or not isinstance(firebase_uid, str):
            raise DeletionHoldInvalid("invalid_write_guard_binding")
        isolation = connection.exec_driver_sql("SHOW TRANSACTION ISOLATION LEVEL").scalar_one()
        if isolation != "read committed":
            raise DeletionHoldInvalid("read_committed_required")
        current_timeout = connection.exec_driver_sql("SHOW lock_timeout").scalar_one()
        # SET LOCAL restores the caller's setting at transaction end. Keep a
        # stricter caller timeout, but never wait indefinitely on a hold writer.
        timeout_ms = _timeout_millis(current_timeout)
        if timeout_ms is None or timeout_ms > GUARD_LOCK_TIMEOUT_MS:
            connection.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{GUARD_LOCK_TIMEOUT_MS}ms"},
            )
        key = advisory_key(user_id)
        connection.execute(
            text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": key}
        )
        # The bounded setting is only for acquiring the shared lock. Restore the
        # caller's transaction setting before any application SQL runs.
        connection.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": current_timeout},
        )
        connection.execute(
            text("SELECT set_config('chirp.deletion_hold_shared_key', :key, true)"), {"key": str(key)}
        )
        row = connection.execute(
            text("SELECT id, firebase_uid FROM users WHERE id = :user_id"), {"user_id": user_id}
        ).first()
        if row is None or row[0] != user_id or row[1] != firebase_uid:
            raise DeletionHoldInvalid("write_guard_identity_changed")
        hold = connection.execute(
            text("SELECT id FROM account_deletion_holds "
                 "WHERE user_id = :user_id AND state = 'active' LIMIT 1"),
            {"user_id": user_id},
        ).first()
        if hold is not None:
            raise DeletionHoldActive("account_deletion_hold_active")
    except DBAPIError:
        # The event runs before SQLAlchemy can finish the transaction start. A
        # direct connection reference may still exist, so invalidate it deliberately;
        # callers must rollback before any subsequent transaction can run.
        sync_session.info[_FAILED_KEY] = True
        connection.invalidate()
        raise DeletionHoldInvalid("write_guard_unavailable") from None
    except BaseException:
        # Preserve cancellation and guard-specific errors, but fail closed first.
        sync_session.info[_FAILED_KEY] = True
        connection.invalidate()
        raise


def bind_account_write_guard(
    session: AsyncSession, *, user_id: uuid.UUID, firebase_uid: str
) -> None:
    """Bind one account identity to this session before its first transaction."""
    uid = _canonical_uuid(user_id)
    fid = _validate_uid(firebase_uid)
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise DeletionHoldInvalid("write_guard_must_be_bound_before_transaction")
    sync_session = session.sync_session
    previous = sync_session.info.get(_INFO_KEY)
    if previous is not None:
        if previous != (uid, fid):
            raise DeletionHoldInvalid("write_guard_rebind_identity")
        return
    sync_session.info[_INFO_KEY] = (uid, fid)
    event.listen(sync_session, "after_begin", _after_begin, once=False)
    event.listen(sync_session, "before_flush", _before_flush, once=False)
    event.listen(sync_session, "before_commit", _before_commit, once=False)
    event.listen(sync_session, "do_orm_execute", _before_orm_execute, once=False)
    event.listen(sync_session, "after_soft_rollback", _after_soft_rollback, once=False)


def is_account_write_guard_bound(session: AsyncSession, *, firebase_uid: str) -> bool:
    """Return whether this session is bound to the supplied Firebase identity."""
    binding = session.sync_session.info.get(_INFO_KEY)
    return isinstance(binding, tuple) and len(binding) == 2 and binding[1] == firebase_uid
