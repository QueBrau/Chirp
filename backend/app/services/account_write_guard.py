"""Session-local account write admission for deletion holds.

The actor and target binding persists across commit and rollback. Every new
transaction acquires its shared gates before application SQL. Event handlers are
attached to the individual session; callers still own commit and rollback.
"""
from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.messaging_limits import MAX_CONVERSATION_INVITEES
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    advisory_key,
)

_INFO_KEY = "c436_account_write_guard"
_FAILED_KEY = "c436_account_write_guard_failed"
_ELIGIBLE_KEY = "c436_account_write_guard_eligible"
GUARD_LOCK_TIMEOUT_MS = 5_000
MAX_GUARDED_ACCOUNTS = MAX_CONVERSATION_INVITEES + 1
MAX_FILTERED_WRITE_CANDIDATES = 500


@dataclass(frozen=True)
class _FilteredBinding:
    """Lock every candidate, but expose only eligible candidates to a bulk writer."""

    user_id: uuid.UUID
    firebase_uid: str
    candidates: tuple[uuid.UUID, ...]


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


def _target_ids(
    actor_id: uuid.UUID, target_user_ids: Iterable[uuid.UUID | str] | None,
) -> tuple[uuid.UUID, ...]:
    targets = {actor_id}
    if target_user_ids is not None:
        try:
            iterator = iter(target_user_ids)
        except TypeError:
            raise DeletionHoldInvalid("invalid_target_set") from None
        for position, value in enumerate(iterator, start=1):
            if position > MAX_GUARDED_ACCOUNTS:
                raise DeletionHoldInvalid("target_set_too_large")
            try:
                targets.add(_canonical_uuid(value))
            except (DeletionHoldInvalid, TypeError):
                raise DeletionHoldInvalid("invalid_target_set") from None
            if len(targets) > MAX_GUARDED_ACCOUNTS:
                raise DeletionHoldInvalid("target_set_too_large")
    return tuple(sorted(targets, key=str))


def _binding_parts(binding: object) -> tuple[uuid.UUID, str, tuple[uuid.UUID, ...]]:
    if not isinstance(binding, tuple) or len(binding) != 3:
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    user_id, firebase_uid = binding[:2]
    if not isinstance(user_id, uuid.UUID) or not isinstance(firebase_uid, str):
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    try:
        _validate_uid(firebase_uid)
    except DeletionHoldInvalid:
        raise DeletionHoldInvalid("invalid_write_guard_binding") from None
    raw_targets = binding[2]
    if not isinstance(raw_targets, tuple):
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    targets = _target_ids(user_id, raw_targets)
    if user_id not in raw_targets or raw_targets != targets:
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    return user_id, firebase_uid, targets


def _advisory_keys(targets: tuple[uuid.UUID, ...]) -> tuple[int, ...]:
    """Deduplicate collision-equivalent keys and impose one global lock order."""
    return tuple(sorted({advisory_key(target) for target in targets}))


def _candidate_ids(values: Iterable[uuid.UUID | str]) -> tuple[uuid.UUID, ...]:
    """Canonical bounded candidate scope, distinct from the strict-message cap."""
    candidates: set[uuid.UUID] = set()
    try:
        for position, value in enumerate(values, start=1):
            if position > MAX_FILTERED_WRITE_CANDIDATES:
                raise DeletionHoldInvalid("target_set_too_large")
            candidates.add(_canonical_uuid(value))
    except (TypeError, AttributeError):
        raise DeletionHoldInvalid("invalid_target_set") from None
    return tuple(sorted(candidates, key=str))


def _filtered_binding_parts(binding: object) -> _FilteredBinding:
    if not isinstance(binding, _FilteredBinding):
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    if not isinstance(binding.user_id, uuid.UUID) or not isinstance(binding.candidates, tuple):
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    _validate_uid(binding.firebase_uid)
    if binding.candidates != _candidate_ids(binding.candidates):
        raise DeletionHoldInvalid("invalid_write_guard_binding")
    return binding


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
        sync_session.info.pop(_ELIGIBLE_KEY, None)


def _after_begin(sync_session: Session, transaction: Any, connection: Any) -> None:
    """Acquire all shared gates and revalidate the bound scope per transaction."""
    if transaction.parent is not None:
        return
    binding = sync_session.info.get(_INFO_KEY)
    try:
        sync_session.info.pop(_ELIGIBLE_KEY, None)
        filtered = isinstance(binding, _FilteredBinding)
        if filtered:
            scope = _filtered_binding_parts(binding)
            user_id, firebase_uid = scope.user_id, scope.firebase_uid
            targets = tuple(sorted({user_id, *scope.candidates}, key=str))
        else:
            user_id, firebase_uid, targets = _binding_parts(binding)
        isolation = connection.exec_driver_sql("SHOW TRANSACTION ISOLATION LEVEL").scalar_one()
        if isolation != "read committed":
            raise DeletionHoldInvalid("read_committed_required")
        current_timeout = connection.exec_driver_sql("SHOW lock_timeout").scalar_one()
        configured_ms = _timeout_millis(current_timeout)
        budget_ms = GUARD_LOCK_TIMEOUT_MS if configured_ms is None else min(configured_ms, GUARD_LOCK_TIMEOUT_MS)
        # One deadline for the whole scope prevents N targets multiplying the
        # wait budget. Restore the caller's setting before application SQL.
        deadline = time.monotonic() + budget_ms / 1000
        for key in _advisory_keys(targets):
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise DeletionHoldInvalid("write_guard_unavailable")
            connection.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{remaining_ms}ms"},
            )
            connection.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": key})
        connection.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": current_timeout},
        )
        connection.execute(
            text("SELECT set_config('chirp.deletion_hold_shared_key', :key, true)"),
            {"key": str(advisory_key(user_id))},
        )
        row = connection.execute(
            text("SELECT id, firebase_uid FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        ).first()
        if row is None or row[0] != user_id or row[1] != firebase_uid:
            raise DeletionHoldInvalid("write_guard_identity_changed")
        target_rows = connection.execute(
            text("SELECT id, firebase_uid FROM users WHERE id IN :target_ids").bindparams(
                bindparam("target_ids", expanding=True)
            ),
            {"target_ids": list(targets)},
        ).all()
        if len(target_rows) != len(targets):
            raise DeletionHoldInvalid("write_guard_target_missing")
        held_ids = set(connection.execute(
            text("SELECT user_id FROM account_deletion_holds "
                 "WHERE user_id IN :target_ids AND state = 'active'").bindparams(
                     bindparam("target_ids", expanding=True)
                 ),
            {"target_ids": list(targets)},
        ).scalars().all())
        if user_id in held_ids or (held_ids and not filtered):
            raise DeletionHoldActive("account_deletion_hold_active")
        if filtered:
            # A released hold does not make a completed deletion stub invitable.
            # Each target gate stays held through the caller's atomic bulk INSERT.
            tombstones = {row[0] for row in target_rows if row[1] == f"deleted:{row[0]}"}
            eligible = frozenset(scope.candidates) - held_ids - tombstones
            sync_session.info[_ELIGIBLE_KEY] = (transaction, eligible)
    except DBAPIError:
        # Transaction startup has not completed. Invalidate even a retained
        # connection reference and require an explicit outer rollback.
        sync_session.info[_FAILED_KEY] = True
        connection.invalidate()
        raise DeletionHoldInvalid("write_guard_unavailable") from None
    except BaseException:
        # Preserve cancellation and guard errors after failing closed.
        sync_session.info[_FAILED_KEY] = True
        connection.invalidate()
        raise


def bind_account_write_guard(
    session: AsyncSession, *, user_id: uuid.UUID, firebase_uid: str,
    target_user_ids: Iterable[uuid.UUID | str] = (),
) -> None:
    """Bind an immutable actor-plus-target scope before the first transaction."""
    uid = _canonical_uuid(user_id)
    fid = _validate_uid(firebase_uid)
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise DeletionHoldInvalid("write_guard_must_be_bound_before_transaction")
    targets = _target_ids(uid, target_user_ids)
    binding = (uid, fid, targets)
    sync_session = session.sync_session
    previous = sync_session.info.get(_INFO_KEY)
    if previous is not None:
        try:
            previous_parts = _binding_parts(previous)
        except DeletionHoldInvalid:
            raise DeletionHoldInvalid("write_guard_rebind_identity") from None
        if previous_parts != (uid, fid, targets):
            raise DeletionHoldInvalid("write_guard_rebind_identity")
        return
    sync_session.info[_INFO_KEY] = binding
    event.listen(sync_session, "after_begin", _after_begin, once=False)
    event.listen(sync_session, "before_flush", _before_flush, once=False)
    event.listen(sync_session, "before_commit", _before_commit, once=False)
    event.listen(sync_session, "do_orm_execute", _before_orm_execute, once=False)
    event.listen(sync_session, "after_soft_rollback", _after_soft_rollback, once=False)


def bind_filtered_account_write_guard(
    session: AsyncSession, *, user_id: uuid.UUID, firebase_uid: str,
    candidate_user_ids: Iterable[uuid.UUID | str],
) -> None:
    """Bind an atomic invitation batch whose held recipients are silently skipped.

    This does not authorize writing to every bound candidate. The writer MUST use
    eligible_account_write_targets in its current transaction before its INSERT.
    The ordinary strict actor/target API and its smaller bound are unchanged.
    """
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise DeletionHoldInvalid("write_guard_must_be_bound_before_transaction")
    binding = _FilteredBinding(
        _canonical_uuid(user_id), _validate_uid(firebase_uid), _candidate_ids(candidate_user_ids)
    )
    sync_session = session.sync_session
    previous = sync_session.info.get(_INFO_KEY)
    if previous is not None:
        if previous != binding:
            raise DeletionHoldInvalid("write_guard_rebind_identity")
        return
    sync_session.info[_INFO_KEY] = binding
    event.listen(sync_session, "after_begin", _after_begin, once=False)
    event.listen(sync_session, "before_flush", _before_flush, once=False)
    event.listen(sync_session, "before_commit", _before_commit, once=False)
    event.listen(sync_session, "do_orm_execute", _before_orm_execute, once=False)
    event.listen(sync_session, "after_soft_rollback", _after_soft_rollback, once=False)


def eligible_account_write_targets(
    session: AsyncSession, *, user_id: uuid.UUID | str,
    candidate_user_ids: Iterable[uuid.UUID | str],
) -> frozenset[uuid.UUID]:
    """Return the exact batch's eligible recipients under its current SQL gates."""
    sync_session = session.sync_session
    _failed(sync_session)
    try:
        scope = _filtered_binding_parts(sync_session.info.get(_INFO_KEY))
        if scope.user_id != _canonical_uuid(user_id) or scope.candidates != _candidate_ids(candidate_user_ids):
            raise DeletionHoldInvalid("write_guard_target_set_changed")
        current = sync_session.get_transaction()
        receipt = sync_session.info.get(_ELIGIBLE_KEY)
        if current is None or not isinstance(receipt, tuple) or len(receipt) != 2 or receipt[0] is not current:
            raise DeletionHoldInvalid("write_guard_transaction_required")
        return receipt[1]
    except DeletionHoldInvalid:
        sync_session.info[_FAILED_KEY] = True
        raise


def is_account_write_guard_bound(
    session: AsyncSession, *, firebase_uid: str,
    target_user_ids: Iterable[uuid.UUID | str] | None = None,
) -> bool:
    """Return whether identity and optional exact target scope are bound."""
    try:
        user_id, bound_uid, targets = _binding_parts(session.sync_session.info.get(_INFO_KEY))
        expected = _target_ids(user_id, target_user_ids)
    except DeletionHoldInvalid:
        return False
    return bound_uid == firebase_uid and targets == expected


def assert_account_write_targets(
    session: AsyncSession, *, user_id: uuid.UUID | str,
    target_user_ids: Iterable[uuid.UUID | str] = (),
) -> None:
    """Reject a route whose fresh actor-plus-target set differs from its binding."""
    try:
        actor_id, _firebase_uid, bound_targets = _binding_parts(
            session.sync_session.info.get(_INFO_KEY)
        )
        if _canonical_uuid(user_id) != actor_id:
            raise DeletionHoldInvalid("write_guard_target_set_changed")
        fresh_targets = _target_ids(actor_id, target_user_ids)
    except DeletionHoldInvalid:
        session.sync_session.info[_FAILED_KEY] = True
        raise DeletionHoldInvalid("write_guard_target_set_changed") from None
    if fresh_targets != bound_targets:
        session.sync_session.info[_FAILED_KEY] = True
        raise DeletionHoldInvalid("write_guard_target_set_changed")
