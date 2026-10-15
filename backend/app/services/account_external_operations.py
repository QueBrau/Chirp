"""Fenced registry for account-scoped external work admitted before provider I/O."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from app import models
from app.db import get_session_factory
from app.services.account_deletion_hold import DeletionHoldInvalid, _lock_exclusive
from app.services.account_write_guard import (
    MAX_GUARDED_ACCOUNTS,
    assert_account_write_targets,
    bind_account_write_guard,
)

OperationKind = Literal["media_finalize_post", "media_finalize_avatar", "campus_verification_email"]
TerminalOperationState = Literal["finished", "uncertain"]
_ALLOWED_KINDS = frozenset({"media_finalize_post", "media_finalize_avatar", "campus_verification_email"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ExternalOperationConflict(Exception):
    """This effect key has already been registered; caller must not repeat it."""


@dataclass(frozen=True)
class ExternalOperationHandle:
    operation_id: uuid.UUID
    actor_user_id: uuid.UUID
    kind: OperationKind
    effect_key: str
    owner_token: uuid.UUID = field(repr=False)
    user_ids: tuple[uuid.UUID, ...] = field(repr=False)


@dataclass(frozen=True)
class RegisteredOperationDrain:
    user_id: uuid.UUID
    request_id: uuid.UUID
    running_count: int
    uncertain_count: int

    @property
    def registered_operations_drained(self) -> bool:
        return self.running_count == 0 and self.uncertain_count == 0


def _canonical_uuid(value: uuid.UUID | str, *, reason: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DeletionHoldInvalid(reason) from None
    if str(value) != str(parsed):
        raise DeletionHoldInvalid(reason)
    return parsed


def _scope_ids(
    actor_id: uuid.UUID, target_user_ids: Iterable[uuid.UUID | str] | None,
) -> tuple[uuid.UUID, ...]:
    ids = {actor_id}
    if target_user_ids is not None:
        try:
            iterator = iter(target_user_ids)
        except TypeError:
            raise DeletionHoldInvalid("invalid_operation_scope") from None
        for position, value in enumerate(iterator, start=1):
            if position > MAX_GUARDED_ACCOUNTS:
                raise DeletionHoldInvalid("operation_scope_too_large")
            ids.add(_canonical_uuid(value, reason="invalid_operation_scope"))
            if len(ids) > MAX_GUARDED_ACCOUNTS:
                raise DeletionHoldInvalid("operation_scope_too_large")
    return tuple(sorted(ids, key=str))


def _validated_kind(value: str) -> OperationKind:
    if not isinstance(value, str) or value not in _ALLOWED_KINDS:
        raise DeletionHoldInvalid("invalid_external_operation_kind")
    return value  # type: ignore[return-value]


def _validated_effect_key(value: str) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise DeletionHoldInvalid("invalid_external_operation_effect_key")
    return value


def _validated_handle(
    handle: ExternalOperationHandle,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, OperationKind, str, tuple[uuid.UUID, ...]]:
    if not isinstance(handle, ExternalOperationHandle):
        raise DeletionHoldInvalid("invalid_external_operation_handle")
    actor_id = _canonical_uuid(handle.actor_user_id, reason="invalid_external_operation_handle")
    operation_id = _canonical_uuid(handle.operation_id, reason="invalid_external_operation_handle")
    token = _canonical_uuid(handle.owner_token, reason="invalid_external_operation_handle")
    if not isinstance(handle.user_ids, tuple) or not handle.user_ids:
        raise DeletionHoldInvalid("invalid_external_operation_handle")
    scope = tuple(
        sorted(
            {_canonical_uuid(value, reason="invalid_external_operation_handle") for value in handle.user_ids},
            key=str,
        )
    )
    if len(scope) != len(handle.user_ids) or actor_id not in scope or len(scope) > MAX_GUARDED_ACCOUNTS:
        raise DeletionHoldInvalid("invalid_external_operation_handle")
    kind = _validated_kind(handle.kind)
    effect_key = _validated_effect_key(handle.effect_key)
    # Keep these validations explicit; an invalid token must not reach a SQL bind.
    if not isinstance(token, uuid.UUID) or not isinstance(operation_id, uuid.UUID):
        raise DeletionHoldInvalid("invalid_external_operation_handle")
    return actor_id, operation_id, token, kind, effect_key, scope


async def register_external_operation(
    *,
    user_id: uuid.UUID,
    firebase_uid: str,
    kind: str,
    effect_key: str,
    target_user_ids: Iterable[uuid.UUID | str] = (),
) -> ExternalOperationHandle:
    """Persist a unique admitted effect before its caller starts provider work.

    A duplicate effect key is never reclaimed, even after completion: retry policy
    belongs to the caller's domain workflow, not this admission ledger.
    """
    actor_id = _canonical_uuid(user_id, reason="invalid_identity")
    operation_kind = _validated_kind(kind)
    key = _validated_effect_key(effect_key)
    scope = _scope_ids(actor_id, target_user_ids)
    operation_id, owner_token = uuid.uuid4(), uuid.uuid4()

    async with get_session_factory()() as session:
        bind_account_write_guard(
            session,
            user_id=actor_id,
            firebase_uid=firebase_uid,
            target_user_ids=scope,
        )
        try:
            assert_account_write_targets(
                session, user_id=actor_id, target_user_ids=scope
            )
            result = await session.execute(
                select(models.User.id, models.User.firebase_uid).where(
                    models.User.id.in_(scope)
                )
            )
            users = {row.id: row.firebase_uid for row in result}
            if len(users) != len(scope):
                raise DeletionHoldInvalid("write_guard_target_missing")
            if users.get(actor_id) != firebase_uid:
                raise DeletionHoldInvalid("write_guard_identity_changed")
            if any(value == f"deleted:{uid}" for uid, value in users.items()):
                raise DeletionHoldInvalid("external_operation_target_deleted")

            inserted_id = await session.scalar(
                pg_insert(models.AccountExternalOperation)
                .values(
                    id=operation_id,
                    actor_user_id=actor_id,
                    kind=operation_kind,
                    effect_key=key,
                    owner_token=owner_token,
                    state="running",
                )
                .on_conflict_do_nothing(
                    index_elements=["actor_user_id", "kind", "effect_key"]
                )
                .returning(models.AccountExternalOperation.id)
            )
            if inserted_id is None:
                raise ExternalOperationConflict("external_operation_already_registered")
            await session.execute(
                pg_insert(models.AccountExternalOperationScope).values(
                    [
                        {"operation_id": operation_id, "user_id": scoped_user_id}
                        for scoped_user_id in scope
                    ]
                )
            )
            await session.commit()
        except (ExternalOperationConflict, DeletionHoldInvalid):
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise DeletionHoldInvalid("external_operation_unavailable") from None

    return ExternalOperationHandle(
        operation_id=operation_id,
        actor_user_id=actor_id,
        kind=operation_kind,
        effect_key=key,
        owner_token=owner_token,
        user_ids=scope,
    )


async def finish_external_operation(
    handle: ExternalOperationHandle,
    *,
    state: TerminalOperationState = "finished",
) -> None:
    """Fenced completion allowed during a hold; no lease expiry or token takeover."""
    actor_id, operation_id, owner_token, kind, effect_key, scope = _validated_handle(handle)
    if not isinstance(state, str) or state not in {"finished", "uncertain"}:
        raise DeletionHoldInvalid("invalid_external_operation_state")

    async with get_session_factory()() as session:
        try:
            row = (await session.execute(
                select(models.AccountExternalOperation)
                .where(models.AccountExternalOperation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if row is None:
                raise DeletionHoldInvalid("external_operation_fence_invalid")
            stored_scope = tuple((await session.scalars(
                select(models.AccountExternalOperationScope.user_id)
                .where(models.AccountExternalOperationScope.operation_id == operation_id)
                .order_by(models.AccountExternalOperationScope.user_id)
            )).all())
            if (
                row.actor_user_id != actor_id
                or row.kind != kind
                or row.effect_key != effect_key
                or row.owner_token != owner_token
                or stored_scope != scope
            ):
                raise DeletionHoldInvalid("external_operation_fence_invalid")
            if row.state == state and row.completed_at is not None:
                await session.rollback()
                return
            if row.state != "running" or row.completed_at is not None:
                raise DeletionHoldInvalid("external_operation_terminal_state")

            changed_id = await session.scalar(
                update(models.AccountExternalOperation)
                .where(
                    models.AccountExternalOperation.id == operation_id,
                    models.AccountExternalOperation.actor_user_id == actor_id,
                    models.AccountExternalOperation.kind == kind,
                    models.AccountExternalOperation.effect_key == effect_key,
                    models.AccountExternalOperation.owner_token == owner_token,
                    models.AccountExternalOperation.state == "running",
                    models.AccountExternalOperation.completed_at.is_(None),
                )
                .values(state=state, completed_at=datetime.now(timezone.utc))
                .returning(models.AccountExternalOperation.id)
                .execution_options(synchronize_session=False)
            )
            if changed_id is None:
                raise DeletionHoldInvalid("external_operation_terminal_state")
            await session.commit()
        except DeletionHoldInvalid:
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise DeletionHoldInvalid("external_operation_unavailable") from None


async def inspect_registered_operation_drain(
    *, user_id: uuid.UUID, request_id: uuid.UUID,
) -> RegisteredOperationDrain:
    """Count registered work under the account's exclusive active-hold gate.

    This only proves the state of operations which were registered in this table;
    it does not prove every producer has been enrolled or a provider outcome was
    externally read back.
    """
    actor_id = _canonical_uuid(user_id, reason="invalid_identity")
    deletion_request_id = _canonical_uuid(request_id, reason="invalid_deletion_request")
    async with get_session_factory()() as session:
        try:
            # Must be the first SQL protocol action; it waits for prior registrations
            # and prevents new registrations while this snapshot is taken.
            await _lock_exclusive(session, actor_id)
            active = await session.scalar(
                select(models.AccountDeletionHold.id).where(
                    models.AccountDeletionHold.user_id == actor_id,
                    models.AccountDeletionHold.request_id == deletion_request_id,
                    models.AccountDeletionHold.state == "active",
                )
            )
            if active is None:
                raise DeletionHoldInvalid("active_deletion_hold_required")
            counts = dict((await session.execute(
                select(
                    models.AccountExternalOperation.state,
                    func.count(models.AccountExternalOperation.id),
                )
                .join(
                    models.AccountExternalOperationScope,
                    models.AccountExternalOperationScope.operation_id
                    == models.AccountExternalOperation.id,
                )
                .where(
                    models.AccountExternalOperationScope.user_id == actor_id,
                    models.AccountExternalOperation.state.in_(("running", "uncertain")),
                )
                .group_by(models.AccountExternalOperation.state)
            )).all())
            await session.commit()
        except DeletionHoldInvalid:
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise DeletionHoldInvalid("external_operation_unavailable") from None
    return RegisteredOperationDrain(
        user_id=actor_id,
        request_id=deletion_request_id,
        running_count=int(counts.get("running", 0)),
        uncertain_count=int(counts.get("uncertain", 0)),
    )
