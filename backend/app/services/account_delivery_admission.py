"""Deletion-aware per-recipient ownership for outbox publication.

The registry is intentionally separate from delivery_outbox: poll coalescing can
delete a queue row while a worker still owns a publish attempt. SQL admission and
completion use short transactions; no connection is kept while a dispatcher awaits
Redis or another transport.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import DeletionHoldInvalid, advisory_key

logger = logging.getLogger(__name__)

RECIPIENT_PREFIX_LIMIT = 128
DELIVERY_ATTEMPT_SLOTS = 32
ATTEMPT_LEASE_SECONDS = 30.0
ADMISSION_TIMEOUT_SECONDS = 5.0
COMPLETION_WRITE_TIMEOUT_SECONDS = 2.0
CANCEL_DRAIN_SECONDS = 0.25
MAX_CONCURRENT_DELIVERIES = 8
MAX_SWEEP_ROWS = 128


@dataclass(frozen=True)
class DeliveryDispatchResult:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    suppressed: int = 0
    uncertain: int = 0
    successful_recipients: int = 0

    def plus(self, other: DeliveryDispatchResult) -> DeliveryDispatchResult:
        return DeliveryDispatchResult(
            claimed=self.claimed + other.claimed,
            delivered=self.delivered + other.delivered,
            retried=self.retried + other.retried,
            dead=self.dead + other.dead,
            suppressed=self.suppressed + other.suppressed,
            uncertain=self.uncertain + other.uncertain,
            successful_recipients=self.successful_recipients + other.successful_recipients,
        )


@dataclass(frozen=True)
class _RecipientPreview:
    outbox_id: uuid.UUID
    recipient_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _Attempt:
    id: uuid.UUID
    user_id: uuid.UUID
    slot: int
    outbox_id: uuid.UUID
    owner_token: uuid.UUID = field(repr=False)


@dataclass(frozen=True)
class _ClaimedRow:
    outbox_id: uuid.UUID
    claim_token: uuid.UUID
    kind: str
    event: dict[str, Any]
    sender_id: str | None
    dispatcher: Any
    attempts: tuple[_Attempt, ...]
    remaining: int
    attempt_number: int
    suppressed: int
    already_owned: int
    capacity_deferred: bool


_late_completion_tasks: set[asyncio.Task[Any]] = set()


def _ordered_account_keys(user_ids: tuple[uuid.UUID, ...]) -> tuple[int, ...]:
    return tuple(sorted({advisory_key(user_id) for user_id in user_ids}))


async def _load_preview(outbox_id: uuid.UUID) -> _RecipientPreview | None:
    """Read only a bounded PostgreSQL array slice, then return the connection."""
    async with get_session_factory()() as session:
        prefix = await session.scalar(
            select(models.DeliveryOutbox.recipient_ids[1:RECIPIENT_PREFIX_LIMIT]).where(
                models.DeliveryOutbox.id == outbox_id
            )
        )
        if prefix is None:
            exists = await session.scalar(
                select(models.DeliveryOutbox.id).where(models.DeliveryOutbox.id == outbox_id)
            )
            if exists is None:
                await session.rollback()
                return None
            prefix = []
        await session.commit()
    return _RecipientPreview(outbox_id, tuple(dict.fromkeys(prefix)))


async def _acquire_recipient_gates(
    session: AsyncSession, recipient_ids: tuple[uuid.UUID, ...],
) -> None:
    isolation = await session.scalar(text("SELECT current_setting('transaction_isolation')"))
    if isolation != "read committed":
        raise DeletionHoldInvalid("read_committed_required")
    # Use the same signed bigint advisory-key order as all account write and hold
    # protocols. Exclusive locks serialize slot allocation and hold activation.
    for key in _ordered_account_keys(recipient_ids):
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def _lock_outbox_row(session: AsyncSession, outbox_id: uuid.UUID) -> bool:
    # Lock only the primary key. Loading an ORM DeliveryOutbox here would transfer
    # an unbounded recipient array into Python, even though we only dispatch a slice.
    row_id = await session.scalar(
        select(models.DeliveryOutbox.id)
        .where(models.DeliveryOutbox.id == outbox_id)
        .with_for_update()
    )
    return row_id is not None


async def _reconstruct_event(
    session: AsyncSession, kind: str, payload: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    sender = payload.get("sender_id")
    sender_id = sender if isinstance(sender, str) else None
    if kind == "poll":
        return payload, sender_id
    if kind != "message":
        return None, sender_id
    try:
        message_id = uuid.UUID(payload["message_id"])
        conversation_id = str(payload["conversation_id"])
        sender_device_id = str(payload["sender_device_id"])
        created_at = str(payload["created_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, sender_id
    message = await session.get(models.Message, message_id)
    if message is None:
        return None, sender_id
    if message.envelope_version is not None:
        return {
            "type": "message",
            "conversation_id": conversation_id,
            "message_id": str(message_id),
            "sender_device_id": sender_device_id,
            "envelope_version": message.envelope_version,
            "created_at": created_at,
        }, sender_id
    if not isinstance(message.ciphertext, (bytes, bytearray, memoryview)):
        return None, sender_id
    return {
        "type": "message",
        "conversation_id": conversation_id,
        "message_id": str(message_id),
        "sender_device_id": sender_device_id,
        "ciphertext": base64.b64encode(bytes(message.ciphertext)).decode("ascii"),
        "created_at": created_at,
    }, sender_id


_QUEUE_ADVANCE = text(
    """
    UPDATE delivery_outbox AS d
       SET recipient_ids =
           COALESCE(ARRAY(
               SELECT item.recipient_id
                 FROM unnest(d.recipient_ids) WITH ORDINALITY AS item(recipient_id, ord)
                WHERE NOT (item.recipient_id = ANY(CAST(:remove_ids AS uuid[])))
                  AND NOT (item.recipient_id = ANY(CAST(:deferred_ids AS uuid[])))
                ORDER BY item.ord
           ), ARRAY[]::uuid[])
           || COALESCE(ARRAY(
               SELECT item.recipient_id
                 FROM unnest(d.recipient_ids) WITH ORDINALITY AS item(recipient_id, ord)
                WHERE item.recipient_id = ANY(CAST(:deferred_ids AS uuid[]))
                ORDER BY item.ord
           ), ARRAY[]::uuid[]),
           claim_token = :claim_token,
           next_attempt_at = now() + make_interval(secs => :lease_seconds),
           attempts = d.attempts + :attempt_increment
     WHERE d.id = :outbox_id
    RETURNING cardinality(d.recipient_ids), d.attempts
    """
).bindparams(
    bindparam("remove_ids", type_=ARRAY(PGUUID(as_uuid=True))),
    bindparam("deferred_ids", type_=ARRAY(PGUUID(as_uuid=True))),
)


async def _claim_and_reserve(
    preview: _RecipientPreview,
    *,
    dispatchers: Mapping[str, Any],
    settings: Settings,
) -> tuple[_ClaimedRow | None, DeliveryDispatchResult]:
    outbox_id = preview.outbox_id
    recipients = preview.recipient_ids
    claim_token = uuid.uuid4()
    async with get_session_factory()() as session:
        try:
            await _acquire_recipient_gates(session, recipients)
            if not await _lock_outbox_row(session, outbox_id):
                await session.rollback()
                return None, DeliveryDispatchResult()
            row = (await session.execute(
                select(
                    models.DeliveryOutbox.id,
                    models.DeliveryOutbox.kind,
                    models.DeliveryOutbox.recipient_ids[1:RECIPIENT_PREFIX_LIMIT],
                    models.DeliveryOutbox.payload,
                    models.DeliveryOutbox.attempts,
                    models.DeliveryOutbox.next_attempt_at,
                    models.DeliveryOutbox.delivered_at,
                    models.DeliveryOutbox.dead_at,
                    models.DeliveryOutbox.__table__.c.claim_token,
                ).where(models.DeliveryOutbox.id == outbox_id)
            )).one_or_none()
            if row is None:
                await session.rollback()
                return None, DeliveryDispatchResult()
            actual_prefix = tuple(dict.fromkeys(row[2] or []))
            if actual_prefix != recipients or row[5] > await session.scalar(select(func.now())):
                await session.rollback()
                return None, DeliveryDispatchResult()
            if row[6] is not None or row[7] is not None:
                await session.rollback()
                return None, DeliveryDispatchResult()

            event, sender_id = await _reconstruct_event(session, row[1], row[3])
            dispatcher = dispatchers.get(row[1])
            if row[1] not in {"message", "poll"} or event is None:
                await session.execute(
                    update(models.DeliveryOutbox.__table__)
                    .where(models.DeliveryOutbox.id == outbox_id)
                    .values(dead_at=func.now(), last_error="source_missing", claim_token=None)
                )
                await session.commit()
                return None, DeliveryDispatchResult(claimed=1, dead=1)
            if dispatcher is None:
                await session.rollback()
                return None, DeliveryDispatchResult()

            if recipients:
                users = dict((await session.execute(
                    select(models.User.id, models.User.firebase_uid)
                    .where(models.User.id.in_(recipients))
                )).all())
                holds = set((await session.scalars(
                    select(models.AccountDeletionHold.user_id).where(
                        models.AccountDeletionHold.user_id.in_(recipients),
                        models.AccountDeletionHold.state == "active",
                    )
                )).all())
                existing_rows = (await session.execute(
                    select(
                        models.AccountDeliveryAttempt.id,
                        models.AccountDeliveryAttempt.user_id,
                        models.AccountDeliveryAttempt.slot,
                        models.AccountDeliveryAttempt.outbox_id,
                        models.AccountDeliveryAttempt.owner_token,
                        models.AccountDeliveryAttempt.state,
                    ).where(models.AccountDeliveryAttempt.user_id.in_(recipients))
                )).all()
            else:
                users, holds, existing_rows = {}, set(), []

            existing_for_pair = {
                (attempt[3], attempt[1]): attempt
                for attempt in existing_rows
                if attempt[3] == outbox_id
            }
            occupied: dict[uuid.UUID, set[int]] = {user_id: set() for user_id in recipients}
            for attempt in existing_rows:
                occupied.setdefault(attempt[1], set()).add(int(attempt[2]))

            suppressed_ids: set[uuid.UUID] = set()
            remove_ids: set[uuid.UUID] = set()
            deferred_ids: list[uuid.UUID] = []
            capacity_deferred = False
            attempts: list[_Attempt] = []
            already_owned = 0
            for recipient_id in recipients:
                firebase_uid = users.get(recipient_id)
                if (firebase_uid is None or recipient_id in holds
                        or firebase_uid == f"deleted:{recipient_id}"):
                    suppressed_ids.add(recipient_id)
                    remove_ids.add(recipient_id)
                    continue
                previous = existing_for_pair.get((outbox_id, recipient_id))
                if previous is not None:
                    remove_ids.add(recipient_id)
                    already_owned += 1
                    continue
                free_slot = next(
                    (slot for slot in range(DELIVERY_ATTEMPT_SLOTS)
                     if slot not in occupied.get(recipient_id, set())),
                    None,
                )
                if free_slot is None:
                    deferred_ids.append(recipient_id)
                    capacity_deferred = True
                    continue
                if len(attempts) >= MAX_CONCURRENT_DELIVERIES:
                    deferred_ids.append(recipient_id)
                    continue
                attempt = _Attempt(
                    id=uuid.uuid4(), user_id=recipient_id, slot=free_slot,
                    outbox_id=outbox_id, owner_token=uuid.uuid4(),
                )
                attempts.append(attempt)
                occupied.setdefault(recipient_id, set()).add(free_slot)
                remove_ids.add(recipient_id)

            for attempt in attempts:
                session.add(models.AccountDeliveryAttempt(
                    id=attempt.id,
                    user_id=attempt.user_id,
                    slot=attempt.slot,
                    outbox_id=attempt.outbox_id,
                    owner_token=attempt.owner_token,
                    state="running",
                ))
            updated = await session.execute(
                _QUEUE_ADVANCE,
                {
                    "remove_ids": list(remove_ids),
                    "deferred_ids": deferred_ids,
                    "claim_token": claim_token,
                    "lease_seconds": ATTEMPT_LEASE_SECONDS,
                    "attempt_increment": 1 if attempts else 0,
                    "outbox_id": outbox_id,
                },
            )
            updated_row = updated.one()
            await session.commit()
            claim = _ClaimedRow(
                outbox_id=outbox_id,
                claim_token=claim_token,
                kind=row[1],
                event=event,
                sender_id=sender_id,
                dispatcher=dispatcher,
                attempts=tuple(attempts),
                remaining=int(updated_row[0] or 0),
                attempt_number=int(updated_row[1]),
                suppressed=len(suppressed_ids),
                already_owned=already_owned,
                capacity_deferred=capacity_deferred,
            )
            return claim, DeliveryDispatchResult(
                claimed=1,
                suppressed=len(suppressed_ids),
                uncertain=already_owned,
            )
        except BaseException:
            await session.rollback()
            raise


async def _mark_uncertain(attempt: _Attempt) -> None:
    async with asyncio.timeout(COMPLETION_WRITE_TIMEOUT_SECONDS):
        async with get_session_factory()() as session:
            await session.execute(
                update(models.AccountDeliveryAttempt)
                .where(
                    models.AccountDeliveryAttempt.id == attempt.id,
                    models.AccountDeliveryAttempt.user_id == attempt.user_id,
                    models.AccountDeliveryAttempt.slot == attempt.slot,
                    models.AccountDeliveryAttempt.outbox_id == attempt.outbox_id,
                    models.AccountDeliveryAttempt.owner_token == attempt.owner_token,
                    models.AccountDeliveryAttempt.state == "running",
                )
                .values(state="uncertain")
            )
            await session.commit()


def _own_late_task(task: asyncio.Task[Any], attempt: _Attempt) -> None:
    """Keep pending provider work strongly owned and leave a review blocker on exit."""
    def completed(done: asyncio.Task[Any]) -> None:
        _late_completion_tasks.discard(done)
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass
        transition = asyncio.create_task(_mark_uncertain(attempt))
        _late_completion_tasks.add(transition)

        def finished(update_task: asyncio.Task[Any]) -> None:
            _late_completion_tasks.discard(update_task)
            try:
                update_task.exception()
            except (asyncio.CancelledError, Exception):
                logger.warning("delivery attempt uncertainty update failed")

        transition.add_done_callback(finished)

    _late_completion_tasks.add(task)
    task.add_done_callback(completed)


def _own_late_finalizer(task: asyncio.Task[Any]) -> None:
    """Keep a shielded SQL finalizer alive through repeated request cancellation."""
    _late_completion_tasks.add(task)

    def completed(done: asyncio.Task[Any]) -> None:
        _late_completion_tasks.discard(done)
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            logger.warning("delivery terminal transaction failed after cancellation")

    task.add_done_callback(completed)


async def _finish_and_release(
    claim: _ClaimedRow,
    successes: tuple[_Attempt, ...],
    uncertain: tuple[_Attempt, ...],
    settings: Settings,
) -> tuple[int, int, bool]:
    """Fence terminal attempt transitions and queue ownership in one transaction."""
    confirmed = 0
    ambiguous = 0
    async with asyncio.timeout(COMPLETION_WRITE_TIMEOUT_SECONDS):
        async with get_session_factory()() as session:
            for attempt in successes:
                result = await session.execute(
                    text("""
                        DELETE FROM account_delivery_attempts
                         WHERE id = :id AND user_id = :user_id AND slot = :slot
                           AND outbox_id = :outbox_id AND owner_token = :owner_token
                           AND state = 'running'
                    """),
                    {
                        "id": attempt.id, "user_id": attempt.user_id,
                        "slot": attempt.slot, "outbox_id": attempt.outbox_id,
                        "owner_token": attempt.owner_token,
                    },
                )
                if result.rowcount == 1:
                    confirmed += 1
                else:
                    raise RuntimeError("delivery attempt success fence did not match")
            for attempt in uncertain:
                result = await session.execute(
                    text("""
                        UPDATE account_delivery_attempts SET state = 'uncertain'
                         WHERE id = :id AND user_id = :user_id AND slot = :slot
                           AND outbox_id = :outbox_id AND owner_token = :owner_token
                           AND state = 'running'
                    """),
                    {
                        "id": attempt.id, "user_id": attempt.user_id,
                        "slot": attempt.slot, "outbox_id": attempt.outbox_id,
                        "owner_token": attempt.owner_token,
                    },
                )
                if result.rowcount == 1:
                    ambiguous += 1
                else:
                    raise RuntimeError("delivery attempt uncertainty fence did not match")

            if claim.remaining == 0:
                queue_result = await session.execute(
                    text("DELETE FROM delivery_outbox WHERE id = :id AND claim_token = :token"),
                    {"id": claim.outbox_id, "token": claim.claim_token},
                )
            else:
                delay = (
                    min(
                        settings.outbox_backoff_cap_s,
                        settings.outbox_backoff_base_s
                        * (2 ** min(max(0, claim.attempt_number), 30)),
                    )
                    if claim.capacity_deferred else 0
                )
                queue_result = await session.execute(
                    text("""
                        UPDATE delivery_outbox
                           SET claim_token = NULL,
                               next_attempt_at = now() + make_interval(secs => :delay)
                         WHERE id = :id AND claim_token = :token
                    """),
                    {"id": claim.outbox_id, "token": claim.claim_token, "delay": delay},
                )
            queue_changed = queue_result.rowcount == 1
            await session.commit()
    return confirmed, ambiguous, queue_changed


async def _finish_owned(
    claim: _ClaimedRow,
    successes: tuple[_Attempt, ...],
    uncertain: tuple[_Attempt, ...],
    settings: Settings,
) -> tuple[tuple[int, int, bool] | None, bool]:
    """Run bounded terminal SQL without letting repeated cancellation detach it."""
    task = asyncio.create_task(_finish_and_release(claim, successes, uncertain, settings))
    try:
        return await asyncio.shield(task), False
    except asyncio.CancelledError:
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=COMPLETION_WRITE_TIMEOUT_SECONDS + 0.5
            )
        except asyncio.CancelledError:
            done = {task} if task.done() else set()
        if task not in done:
            _own_late_finalizer(task)
            return None, True
        try:
            return task.result(), True
        except (asyncio.CancelledError, Exception):
            # Cancellation of the caller remains authoritative even if its
            # shielded terminal transaction fails. `_run_claim` will preserve
            # the registered attempt as a blocker and propagate cancellation.
            logger.warning("delivery finalizer failed after caller cancellation")
            return None, True


def _is_success(task: asyncio.Task[Any], recipient_id: uuid.UUID) -> bool:
    if task.cancelled():
        return False
    try:
        result = task.result()
    except BaseException:
        return False
    return type(result) is list and result == []


async def _release_claim(claim: _ClaimedRow, settings: Settings) -> bool:
    """Release the queue token or retire the exhausted row, fenced against coalescing."""
    try:
        async with asyncio.timeout(COMPLETION_WRITE_TIMEOUT_SECONDS):
            async with get_session_factory()() as session:
                if claim.remaining == 0:
                    result = await session.execute(
                        text("DELETE FROM delivery_outbox WHERE id = :id AND claim_token = :token"),
                        {"id": claim.outbox_id, "token": claim.claim_token},
                    )
                else:
                    delay = (
                        min(
                            settings.outbox_backoff_cap_s,
                            settings.outbox_backoff_base_s
                            * (2 ** min(max(0, claim.attempt_number), 30)),
                        )
                        if claim.capacity_deferred else 0
                    )
                    result = await session.execute(
                        text("""
                            UPDATE delivery_outbox
                               SET claim_token = NULL,
                                   next_attempt_at = now() + make_interval(secs => :delay)
                             WHERE id = :id AND claim_token = :token
                        """),
                        {"id": claim.outbox_id, "token": claim.claim_token, "delay": delay},
                    )
                await session.commit()
    except Exception:
        logger.warning("delivery outbox claim release failed row_id=%s", claim.outbox_id)
        return False
    return bool(result.rowcount)


async def _run_claim(claim: _ClaimedRow, settings: Settings) -> DeliveryDispatchResult:
    tasks: dict[asyncio.Task[Any], _Attempt] = {
        asyncio.create_task(
            claim.dispatcher([attempt.user_id], claim.event, claim.sender_id)
        ): attempt
        for attempt in claim.attempts
    }
    if not tasks:
        released = await _release_claim(claim, settings)
        return DeliveryDispatchResult(
            claimed=1,
            retried=1 if claim.remaining and released else 0,
            suppressed=claim.suppressed,
            uncertain=claim.already_owned,
            dead=0,
        )

    canceled = False
    try:
        done, pending = await asyncio.wait(
            tasks, timeout=max(0.001, settings.outbox_dispatch_timeout_s)
        )
        if pending:
            for task in pending:
                task.cancel()
            drained, pending = await asyncio.wait(pending, timeout=CANCEL_DRAIN_SECONDS)
            done |= drained
    except asyncio.CancelledError:
        canceled = True
        pending = {task for task in tasks if not task.done()}
        for task in pending:
            task.cancel()
        try:
            if pending:
                drained, pending = await asyncio.wait(pending, timeout=CANCEL_DRAIN_SECONDS)
            else:
                drained = set()
        except asyncio.CancelledError:
            # Preserve ownership and let a done callback write uncertain. The
            # attempt remains running and blocks deletion if this write never lands.
            drained, pending = set(), {task for task in tasks if not task.done()}
        done = {task for task in tasks if task.done()} | drained

    successes: list[_Attempt] = []
    uncertain: list[_Attempt] = []
    owned_pending: list[tuple[asyncio.Task[Any], _Attempt]] = []
    for task, attempt in tasks.items():
        if task in pending and not task.done():
            owned_pending.append((task, attempt))
            continue
        if task in done and _is_success(task, attempt.user_id):
            successes.append(attempt)
        else:
            uncertain.append(attempt)

    for task, attempt in owned_pending:
        _own_late_task(task, attempt)
    try:
        finished, cancelled_during_finish = await _finish_owned(
            claim, tuple(successes), tuple(uncertain), settings
        )
        canceled = canceled or cancelled_during_finish
        if finished is None:
            confirmed, ambiguous, queue_changed = 0, len(tasks), False
        else:
            confirmed, ambiguous, queue_changed = finished
    except Exception:
        # A failed finish leaves the current row running, which remains a deletion
        # blocker. Queue ownership is not released, preventing stale replay.
        logger.warning("delivery attempt completion write failed row_id=%s", claim.outbox_id)
        confirmed, ambiguous, queue_changed = 0, len(tasks), False
    if canceled:
        raise asyncio.CancelledError
    return DeliveryDispatchResult(
        claimed=1,
        delivered=int(
            queue_changed and claim.remaining == 0 and confirmed == len(claim.attempts)
            and ambiguous == 0 and not owned_pending and claim.suppressed == 0
            and claim.already_owned == 0
        ),
        retried=1 if claim.remaining and queue_changed else 0,
        suppressed=claim.suppressed,
        uncertain=ambiguous + len(owned_pending) + claim.already_owned,
        successful_recipients=confirmed,
    )


async def dispatch_admitted_outbox_row(
    outbox_id: uuid.UUID,
    *,
    dispatchers: Mapping[str, Any],
    settings: Settings | None = None,
) -> DeliveryDispatchResult:
    """Admit and publish one row through the same bounded path for live and sweepers."""
    configured = settings or get_settings()
    async with asyncio.timeout(ADMISSION_TIMEOUT_SECONDS):
        preview = await _load_preview(outbox_id)
        if preview is None:
            return DeliveryDispatchResult()
        claim, early = await _claim_and_reserve(
            preview, dispatchers=dispatchers, settings=configured
        )
    if claim is None:
        return early
    return await _run_claim(claim, configured)


async def dispatch_admitted_pending(
    limit: int,
    *,
    dispatchers: Mapping[str, Any],
    settings: Settings | None = None,
) -> DeliveryDispatchResult:
    """Select bounded row IDs only; each row then uses the shared live/sweep path."""
    configured = settings or get_settings()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("outbox dispatch limit must be a positive integer")
    bounded_limit = min(limit, MAX_SWEEP_ROWS)
    async with asyncio.timeout(ADMISSION_TIMEOUT_SECONDS):
        async with get_session_factory()() as session:
            row_ids = list((await session.scalars(
                select(models.DeliveryOutbox.id)
                .where(
                    models.DeliveryOutbox.next_attempt_at <= func.now(),
                    models.DeliveryOutbox.delivered_at.is_(None),
                    models.DeliveryOutbox.dead_at.is_(None),
                )
                .order_by(models.DeliveryOutbox.next_attempt_at, models.DeliveryOutbox.created_at)
                .limit(bounded_limit)
            )).all())
            await session.commit()
    total = DeliveryDispatchResult()
    for row_id in row_ids:
        total = total.plus(await dispatch_admitted_outbox_row(
            row_id, dispatchers=dispatchers, settings=configured
        ))
    return total
