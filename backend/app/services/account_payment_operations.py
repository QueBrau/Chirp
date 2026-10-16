"""Journal stable payment effects and owner-fenced Stripe I/O attempts."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.db import get_session_factory
from app.services.account_external_operations import (
    ExternalOperationHandle,
    _validated_handle,
)

PaymentEffectKind = Literal[
    "connect_account_create",
    "connect_account_link",
    "customer_create",
    "customer_session_create",
    "payment_intent_create",
    "payment_intent_cancel",
]
_ALLOWED_KINDS = frozenset({
    "connect_account_create",
    "connect_account_link",
    "customer_create",
    "customer_session_create",
    "payment_intent_create",
    "payment_intent_cancel",
})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_ID = re.compile(r"^(?:acct|cus|pi|seti|cs|ca|evt|in)_[A-Za-z0-9]{1,240}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9:_-]{1,255}$")
MAX_UNRESOLVED_ATTEMPTS = 8
CREATE_RETRY_HORIZON = timedelta(hours=23)
_CREATE_KINDS = frozenset({"connect_account_create", "customer_create"})
_REQUIRED_PROVIDER_PREFIX = {
    "connect_account_create": "acct_",
    "customer_create": "cus_",
    "payment_intent_create": "pi_",
    "payment_intent_cancel": "pi_",
}


class PaymentOperationConflict(Exception):
    """A matching effect exists but cannot safely be replayed or rebound."""


class PaymentOperationInvalid(Exception):
    """A malformed or stale payment-operation handle failed closed."""


@dataclass(frozen=True)
class PaymentEffectHandle:
    effect_id: uuid.UUID
    kind: PaymentEffectKind
    effect_key: str
    request_fingerprint: str
    idempotency_key: str = field(repr=False)
    connected_account_id: str | None = None
    reservation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PaymentAttemptHandle:
    attempt_id: uuid.UUID
    effect_id: uuid.UUID
    parent_operation_id: uuid.UUID
    actor_user_id: uuid.UUID
    owner_token: uuid.UUID = field(repr=False)


def _uuid(value: object, reason: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise PaymentOperationInvalid(reason) from None
    if str(value) != str(parsed):
        raise PaymentOperationInvalid(reason)
    return parsed


def _effect_values(
    *,
    kind: str,
    effect_key: str,
    request_fingerprint: str,
    idempotency_key: str,
    connected_account_id: str | None,
    reservation_id: uuid.UUID | None,
) -> tuple[PaymentEffectKind, str, str, str, str | None, uuid.UUID | None]:
    if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
        raise PaymentOperationInvalid("invalid_payment_effect_kind")
    if not isinstance(effect_key, str) or _SHA256_HEX.fullmatch(effect_key) is None:
        raise PaymentOperationInvalid("invalid_payment_effect_key")
    if not isinstance(request_fingerprint, str) or _SHA256_HEX.fullmatch(request_fingerprint) is None:
        raise PaymentOperationInvalid("invalid_payment_request_fingerprint")
    if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise PaymentOperationInvalid("invalid_payment_idempotency_key")
    if connected_account_id is not None and (
        not isinstance(connected_account_id, str)
        or not connected_account_id.startswith("acct_")
        or _PROVIDER_ID.fullmatch(connected_account_id) is None
    ):
        raise PaymentOperationInvalid("invalid_payment_connected_account")
    if reservation_id is not None:
        reservation_id = _uuid(reservation_id, "invalid_payment_reservation")
    requires_connected_account = kind in {
        "connect_account_link", "customer_create", "customer_session_create",
        "payment_intent_create", "payment_intent_cancel",
    }
    requires_reservation = kind in {
        "payment_intent_create", "payment_intent_cancel",
    }
    if (connected_account_id is not None) != requires_connected_account:
        raise PaymentOperationInvalid("invalid_payment_effect_scope")
    if (reservation_id is not None) != requires_reservation:
        raise PaymentOperationInvalid("invalid_payment_effect_scope")
    return (
        kind, effect_key, request_fingerprint, idempotency_key,
        connected_account_id, reservation_id,
    )  # type: ignore[return-value]


def _validate_parent(parent: ExternalOperationHandle):
    try:
        actor, operation_id, token, kind, key, scope = _validated_handle(parent)
    except Exception:
        raise PaymentOperationInvalid("invalid_payment_parent_handle") from None
    if kind != "payment_request":
        raise PaymentOperationInvalid("payment_parent_required")
    return actor, operation_id, token, kind, key, scope


def _validate_effect_handle(handle: PaymentEffectHandle) -> None:
    if not isinstance(handle, PaymentEffectHandle):
        raise PaymentOperationInvalid("invalid_payment_effect_handle")
    _uuid(handle.effect_id, "invalid_payment_effect_handle")
    values = _effect_values(
        kind=handle.kind,
        effect_key=handle.effect_key,
        request_fingerprint=handle.request_fingerprint,
        idempotency_key=handle.idempotency_key,
        connected_account_id=handle.connected_account_id,
        reservation_id=handle.reservation_id,
    )
    if values[0] != handle.kind:
        raise PaymentOperationInvalid("invalid_payment_effect_handle")


def _validate_attempt_handle(handle: PaymentAttemptHandle) -> None:
    if not isinstance(handle, PaymentAttemptHandle):
        raise PaymentOperationInvalid("invalid_payment_attempt_handle")
    _uuid(handle.attempt_id, "invalid_payment_attempt_handle")
    _uuid(handle.effect_id, "invalid_payment_attempt_handle")
    _uuid(handle.parent_operation_id, "invalid_payment_attempt_handle")
    _uuid(handle.actor_user_id, "invalid_payment_attempt_handle")
    _uuid(handle.owner_token, "invalid_payment_attempt_handle")


async def _assert_parent_running(
    session: AsyncSession,
    parent: ExternalOperationHandle,
    *,
    lock_parent: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    actor_id, operation_id, owner_token, kind, effect_key, scope = _validate_parent(parent)
    operation_query = select(models.AccountExternalOperation).where(
        models.AccountExternalOperation.id == operation_id
    )
    if lock_parent:
        operation_query = operation_query.with_for_update()
    operation = await session.scalar(operation_query.execution_options(populate_existing=True))
    if (
        operation is None
        or operation.actor_user_id != actor_id
        or operation.kind != kind
        or operation.effect_key != effect_key
        or operation.owner_token != owner_token
        or operation.state != "running"
        or operation.completed_at is not None
    ):
        raise PaymentOperationInvalid("payment_parent_not_running")
    stored_scope = tuple((await session.scalars(
        select(models.AccountExternalOperationScope.user_id)
        .where(models.AccountExternalOperationScope.operation_id == operation_id)
        .order_by(models.AccountExternalOperationScope.user_id)
    )).all())
    if stored_scope != scope or actor_id not in stored_scope:
        raise PaymentOperationInvalid("payment_parent_scope_invalid")
    return actor_id, operation_id


async def _assert_parent_identity(
    session: AsyncSession,
    parent: ExternalOperationHandle,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Validate exact durable parent fields without requiring it remain running."""
    actor_id, operation_id, owner_token, kind, effect_key, scope = _validate_parent(parent)
    operation = await session.scalar(
        select(models.AccountExternalOperation)
        .where(models.AccountExternalOperation.id == operation_id)
        .execution_options(populate_existing=True)
    )
    if (
        operation is None
        or operation.actor_user_id != actor_id
        or operation.kind != kind
        or operation.effect_key != effect_key
        or operation.owner_token != owner_token
    ):
        raise PaymentOperationInvalid("payment_parent_fence_invalid")
    stored_scope = tuple((await session.scalars(
        select(models.AccountExternalOperationScope.user_id)
        .where(models.AccountExternalOperationScope.operation_id == operation_id)
        .order_by(models.AccountExternalOperationScope.user_id)
    )).all())
    if stored_scope != scope or actor_id not in stored_scope:
        raise PaymentOperationInvalid("payment_parent_scope_invalid")
    return actor_id, operation_id


def _effect_matches(row: models.AccountPaymentEffect, values: tuple) -> bool:
    return (
        row.kind == values[0]
        and row.effect_key == values[1]
        and row.request_fingerprint == values[2]
        and row.idempotency_key == values[3]
        and row.connected_account_id == values[4]
        and row.reservation_id == values[5]
    )


async def register_payment_effect(
    parent: ExternalOperationHandle,
    *,
    kind: PaymentEffectKind,
    effect_key: str,
    request_fingerprint: str,
    idempotency_key: str,
    connected_account_id: str | None = None,
    reservation_id: uuid.UUID | None = None,
) -> PaymentEffectHandle:
    """Create or validate a global immutable logical effect under a live request."""
    values = _effect_values(
        kind=kind,
        effect_key=effect_key,
        request_fingerprint=request_fingerprint,
        idempotency_key=idempotency_key,
        connected_account_id=connected_account_id,
        reservation_id=reservation_id,
    )
    _validate_parent(parent)
    async with get_session_factory()() as session:
        try:
            await _assert_parent_running(session, parent, lock_parent=True)
            await session.execute(
                pg_insert(models.AccountPaymentEffect)
                .values(
                    kind=values[0],
                    effect_key=values[1],
                    request_fingerprint=values[2],
                    idempotency_key=values[3],
                    connected_account_id=values[4],
                    reservation_id=values[5],
                )
                .on_conflict_do_nothing(index_elements=["kind", "effect_key"])
            )
            row = (await session.execute(
                select(models.AccountPaymentEffect)
                .where(
                    models.AccountPaymentEffect.kind == values[0],
                    models.AccountPaymentEffect.effect_key == values[1],
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if row is None:
                raise PaymentOperationInvalid("payment_effect_unavailable")
            if not _effect_matches(row, values):
                raise PaymentOperationConflict("payment_effect_binding_mismatch")
            await session.commit()
        except (PaymentOperationConflict, PaymentOperationInvalid):
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise PaymentOperationInvalid("payment_effect_unavailable") from None
    return PaymentEffectHandle(
        effect_id=row.id,
        kind=values[0],
        effect_key=values[1],
        request_fingerprint=values[2],
        idempotency_key=values[3],
        connected_account_id=values[4],
        reservation_id=values[5],
    )


async def register_payment_attempt(
    parent: ExternalOperationHandle,
    effect: PaymentEffectHandle,
) -> PaymentAttemptHandle:
    """Persist a new I/O owner before provider work; never take over old work."""
    _validate_parent(parent)
    _validate_effect_handle(effect)
    actor_id, operation_id, *_ = _validate_parent(parent)
    attempt_id, token = uuid.uuid4(), uuid.uuid4()
    async with get_session_factory()() as session:
        try:
            # Parent then effect is the registry lock order. Generic parent
            # completion takes the parent lock before checking running children.
            await _assert_parent_running(session, parent, lock_parent=True)
            effect_row = (await session.execute(
                select(models.AccountPaymentEffect)
                .where(models.AccountPaymentEffect.id == effect.effect_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if effect_row is None or not _effect_matches(effect_row, (
                effect.kind, effect.effect_key, effect.request_fingerprint,
                effect.idempotency_key, effect.connected_account_id,
                effect.reservation_id,
            )):
                raise PaymentOperationInvalid("payment_effect_fence_invalid")
            unresolved = list((await session.scalars(
                select(models.AccountPaymentAttempt).where(
                    models.AccountPaymentAttempt.effect_id == effect.effect_id,
                    models.AccountPaymentAttempt.state.in_(("running", "uncertain")),
                )
            )).all())
            if any(row.state == "running" for row in unresolved):
                raise PaymentOperationConflict("payment_attempt_already_running")
            if len(unresolved) >= MAX_UNRESOLVED_ATTEMPTS:
                raise PaymentOperationConflict("payment_attempt_review_required")
            prior_terminal = await session.scalar(
                select(models.AccountPaymentAttempt.id).where(
                    models.AccountPaymentAttempt.effect_id == effect.effect_id,
                    models.AccountPaymentAttempt.state.in_(("finished", "reconciled")),
                ).limit(1)
            )
            if effect_row.provider_object_id is not None and effect.kind != "payment_intent_cancel":
                raise PaymentOperationConflict("payment_effect_already_confirmed")
            if prior_terminal is not None and effect.kind != "payment_intent_cancel":
                raise PaymentOperationConflict("payment_effect_already_confirmed")
            if effect.kind in _CREATE_KINDS and unresolved:
                first_attempt = await session.scalar(
                    select(func.min(models.AccountPaymentAttempt.created_at)).where(
                        models.AccountPaymentAttempt.effect_id == effect.effect_id
                    )
                )
                if first_attempt is not None and first_attempt <= datetime.now(timezone.utc) - CREATE_RETRY_HORIZON:
                    raise PaymentOperationConflict("payment_create_retry_window_expired")

            session.add(models.AccountPaymentAttempt(
                id=attempt_id,
                effect_id=effect.effect_id,
                parent_operation_id=operation_id,
                actor_user_id=actor_id,
                owner_token=token,
                state="running",
            ))
            await session.commit()
        except (PaymentOperationConflict, PaymentOperationInvalid):
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise PaymentOperationInvalid("payment_attempt_unavailable") from None
    return PaymentAttemptHandle(
        attempt_id=attempt_id,
        effect_id=effect.effect_id,
        parent_operation_id=operation_id,
        actor_user_id=actor_id,
        owner_token=token,
    )


async def _locked_attempt(
    session: AsyncSession,
    parent: ExternalOperationHandle,
    effect: PaymentEffectHandle,
    attempt: PaymentAttemptHandle,
) -> tuple[models.AccountPaymentEffect, models.AccountPaymentAttempt]:
    actor_id, parent_id = await _assert_parent_identity(session, parent)
    _validate_effect_handle(effect)
    _validate_attempt_handle(attempt)
    if (
        attempt.effect_id != effect.effect_id
        or attempt.parent_operation_id != parent_id
        or attempt.actor_user_id != actor_id
    ):
        raise PaymentOperationInvalid("payment_attempt_fence_invalid")
    effect_row = (await session.execute(
        select(models.AccountPaymentEffect)
        .where(models.AccountPaymentEffect.id == effect.effect_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    attempt_row = (await session.execute(
        select(models.AccountPaymentAttempt)
        .where(models.AccountPaymentAttempt.id == attempt.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    expected = (
        effect.kind, effect.effect_key, effect.request_fingerprint,
        effect.idempotency_key, effect.connected_account_id, effect.reservation_id,
    )
    if (
        effect_row is None
        or not _effect_matches(effect_row, expected)
        or attempt_row is None
        or attempt_row.effect_id != effect.effect_id
        or attempt_row.parent_operation_id != parent_id
        or attempt_row.actor_user_id != actor_id
        or attempt_row.owner_token != attempt.owner_token
    ):
        raise PaymentOperationInvalid("payment_attempt_fence_invalid")
    return effect_row, attempt_row


async def finish_payment_attempt(
    session: AsyncSession,
    parent: ExternalOperationHandle,
    effect: PaymentEffectHandle,
    attempt: PaymentAttemptHandle,
    *,
    provider_id: str | None = None,
    reconcile_uncertain: bool = True,
) -> None:
    """Terminalize confirmed work in the caller's transaction with domain binding.

    This function flushes but never commits; the route must commit it atomically
    with the customer/account/reservation state that the Stripe result authorizes.
    """
    if provider_id is not None and (
        not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None
    ):
        raise PaymentOperationInvalid("invalid_payment_provider_id")
    if type(reconcile_uncertain) is not bool:
        raise PaymentOperationInvalid("invalid_payment_reconciliation_mode")
    try:
        effect_row, attempt_row = await _locked_attempt(session, parent, effect, attempt)
        if attempt_row.state != "running" or attempt_row.completed_at is not None:
            raise PaymentOperationConflict("payment_attempt_not_running")
        required_prefix = _REQUIRED_PROVIDER_PREFIX.get(effect.kind)
        if required_prefix is not None and (
            provider_id is None or not provider_id.startswith(required_prefix)
        ):
            raise PaymentOperationInvalid("payment_provider_binding_required")
        if effect.kind in {"connect_account_link", "customer_session_create"} and provider_id is not None:
            raise PaymentOperationInvalid("payment_capability_value_forbidden")
        if (
            provider_id is not None
            and effect_row.provider_object_id is not None
            and effect_row.provider_object_id != provider_id
        ):
            raise PaymentOperationConflict("payment_provider_binding_mismatch")
        if provider_id is not None:
            effect_row.provider_object_id = provider_id
        attempt_row.state = "finished"
        attempt_row.completed_at = datetime.now(timezone.utc)
        if reconcile_uncertain:
            rows = (await session.scalars(
                select(models.AccountPaymentAttempt)
                .where(
                    models.AccountPaymentAttempt.effect_id == effect.effect_id,
                    models.AccountPaymentAttempt.state == "uncertain",
                )
                .with_for_update()
            )).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                row.state = "reconciled"
                row.completed_at = now
        await session.flush()
    except (PaymentOperationConflict, PaymentOperationInvalid):
        raise
    except DBAPIError:
        raise PaymentOperationInvalid("payment_attempt_completion_unavailable") from None


async def mark_payment_attempt_uncertain(
    parent: ExternalOperationHandle,
    effect: PaymentEffectHandle,
    attempt: PaymentAttemptHandle,
) -> None:
    """Fence a failed or ambiguous provider outcome using a separate short session."""
    _validate_parent(parent)
    _validate_effect_handle(effect)
    _validate_attempt_handle(attempt)
    async with get_session_factory()() as session:
        try:
            _, row = await _locked_attempt(session, parent, effect, attempt)
            if row.state == "uncertain" and row.completed_at is not None:
                await session.rollback()
                return
            if row.state != "running" or row.completed_at is not None:
                raise PaymentOperationConflict("payment_attempt_terminal")
            row.state = "uncertain"
            row.completed_at = datetime.now(timezone.utc)
            await session.commit()
        except (PaymentOperationConflict, PaymentOperationInvalid):
            await session.rollback()
            raise
        except DBAPIError:
            await session.rollback()
            raise PaymentOperationInvalid("payment_attempt_uncertainty_unavailable") from None


async def inspect_payment_operation_drain(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> dict[str, int]:
    """Count unresolved payment attempts for an account in an existing gate txn."""
    actor_id = _uuid(user_id, "invalid_payment_drain_user")
    rows = await session.execute(
        select(models.AccountPaymentAttempt.state, func.count(models.AccountPaymentAttempt.id))
        .where(
            models.AccountPaymentAttempt.actor_user_id == actor_id,
            models.AccountPaymentAttempt.state.in_(("running", "uncertain")),
        )
        .group_by(models.AccountPaymentAttempt.state)
    )
    result = {"running": 0, "uncertain": 0}
    for state, count in rows:
        if state in result:
            result[state] = int(count)
    return result
