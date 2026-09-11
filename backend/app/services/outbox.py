"""Durable delivery ledger and lease-based retry sweeper (board card c356).

`enqueue` writes a delivery_outbox row into the CALLER's own transaction, so a
domain write (e.g. inserting a Message) and the intent to deliver it commit or
roll back together -- a crash after that commit but before delivery is recoverable,
because the row survives. `dispatch_now` is the live/fast path: called right after
the caller's commit, with the in-memory event already built (so it costs zero extra
DB reads), using the caller's OWN session for its short follow-up write only -- no
second connection is checked out on the request path. `dispatch_pending` is the
sweeper: it NEVER holds a database session, connection, or row lock across the
publish attempt (Redis I/O). chirp-ws runs with a pool of size 1 (DB_POOL_SIZE=1,
DB_MAX_OVERFLOW=1 -- infra/deployment.json, services.ws.env) and this sweeper runs on
every instance, so holding a connection open for up to outbox_dispatch_timeout_s
would starve the gateway's own periodic SQL. Instead it uses a LEASE:

  1. One short transaction claims a batch: `UPDATE ... SET next_attempt_at = now() +
     lease, attempts = attempts + 1 WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED
     LIMIT :n) RETURNING ...` (same FOR UPDATE SKIP LOCKED shape as
     app/services/prekey_service.py). For kind='message' rows, the Message's
     ciphertext is hydrated inside this SAME transaction (one SELECT before commit)
     so nothing needs a session afterwards; a missing Message row is marked dead
     with last_error='source_missing' instead of being retried, since retrying a
     row whose source no longer exists cannot ever succeed. Commit releases the
     connection AND the row lock immediately; next_attempt_at being pushed into the
     future is the lease -- no lock is held while Redis is called.
  2. The publish attempt runs with NO session at all, bounded by
     outbox_dispatch_timeout_s, through the dispatcher registered for that kind.
  3. A second short transaction records the outcome: full success deletes the row;
     partial/total failure narrows recipient_ids to the still-failing subset,
     bumps next_attempt_at by an exponential backoff, and sets a short last_error
     code (never ciphertext, never raw exception text -- it can carry a transport
     credential); attempts >= outbox_max_attempts sets dead_at instead and logs a
     warning naming only the row id.

A crash between steps 1 and 2/3 leaves the row exactly as the lease set it: attempts
already bumped, next_attempt_at in the future. Once the lease expires the row is
claimed again -- an at-least-once retry, not an at-most-once guarantee. That is
covered by the client's own message_id dedupe (app-mobile
app/(tabs)/messages/[id].tsx, around line 160), not by anything here. See
DELIVERY-OUTBOX.md for the full STORED/DELIVERED/READ contract.

Polls are wired into this module as of board card c345: `app/routers/polls.py`
registers a 'poll' dispatcher (its module-level `_broadcast`) at import time via
`register_dispatcher`, and its four write routes (create/vote/close/delete) each
enqueue a 'poll' row before commit and call `dispatch_now` after, the same shape
`send_message` uses for 'message' rows. The one real difference from 'message':
a 'poll' row's payload already IS the full event to publish -- the aggregate
snapshot `_prepare_broadcast` built at enqueue time -- so `dispatch_pending`'s
poll branch, below, uses the payload directly with no hydration step and no
`source_missing` dead-letter path (there is no separate source table a poll row
could outlive). polls.py also coalesces at enqueue time to at most one pending
row per poll; see its `_enqueue_poll_event` and DELIVERY-OUTBOX.md.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session_factory

logger = logging.getLogger(__name__)

# recipient_ids, event, sender_id -> the recipient_ids that failed (empty means full
# success). sender_id is passed OUT OF BAND from the event dict -- the event dict is
# exactly what goes out over the wire to every recipient, and must never carry a
# sender user id (see the module docstring and messages.py's c344 docstring: a
# recipient never learns a sender's user id beyond what ConversationMemberOut already
# exposes). The dispatcher may still use sender_id itself, e.g. to skip the
# content-free push to the sender's own other devices.
Dispatcher = Callable[[list[uuid.UUID], dict, str | None], Awaitable[list[str]]]

_dispatchers: dict[str, Dispatcher] = {}


def register_dispatcher(kind: str, fn: Dispatcher) -> None:
    """Bind the per-recipient publish function for `kind`. Call once at router import time.

    Kept as a registry (not a direct import) so this module never imports the router
    module that owns the real publish call -- app/routers/messages.py binds
    `publish_to_user`/`send_content_free_push` at ITS OWN module level, and
    tests/test_contact_blocks.py's monkeypatch of `messages_router.publish_to_user`
    depends on the live call still going through that exact name.
    """
    _dispatchers[kind] = fn


@dataclass(frozen=True)
class DispatchStats:
    claimed: int
    delivered: int
    retried: int
    dead: int


def _backoff_seconds(attempts: int) -> float:
    settings = get_settings()
    return min(settings.outbox_backoff_cap_s, settings.outbox_backoff_base_s * (2**attempts))


async def enqueue(
    session: AsyncSession, *, kind: str, recipient_ids: list[uuid.UUID], payload: dict
) -> uuid.UUID:
    """Add a pending delivery row into the CALLER's own transaction; caller commits."""
    row = models.DeliveryOutbox(kind=kind, recipient_ids=list(recipient_ids), payload=payload)
    session.add(row)
    await session.flush()
    return row.id


async def dispatch_now(
    session: AsyncSession,
    row_id: uuid.UUID,
    *,
    kind: str,
    recipient_ids: list[uuid.UUID],
    event: dict,
    sender_id: str | None = None,
) -> None:
    """Best-effort immediate delivery on the request path.

    Reuses the request's OWN session for the follow-up write -- no second
    connection is checked out here. Any exception raised by calling the dispatcher
    itself (as opposed to a per-recipient failure, which the dispatcher already
    catches and reports back as a failed-id list) leaves the row exactly as
    enqueued, untouched, for the sweeper to pick up on its own schedule later.

    sender_id is passed separately from `event` -- `event` is exactly what goes out
    over the wire to every recipient and must never carry a sender user id (c356 fix;
    see the Dispatcher type comment).
    """
    dispatcher = _dispatchers.get(kind)
    if dispatcher is None:
        return
    try:
        failed = await dispatcher(recipient_ids, event, sender_id)
    except Exception:
        logger.warning("outbox live dispatch raised kind=%s row_id=%s", kind, row_id)
        return
    if not failed:
        await session.execute(delete(models.DeliveryOutbox).where(models.DeliveryOutbox.id == row_id))
        await session.commit()
        return
    row = await session.get(models.DeliveryOutbox, row_id)
    if row is None:
        return
    attempts = row.attempts + 1
    if attempts >= get_settings().outbox_max_attempts:
        row.dead_at = datetime.now(timezone.utc)
        row.last_error = "attempts_exhausted"
    else:
        row.recipient_ids = [uuid.UUID(rid) for rid in failed]
        row.attempts = attempts
        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(attempts))
        row.last_error = "publish_failed"
    await session.commit()


_CLAIM_SQL = text(
    """
    UPDATE delivery_outbox
    SET next_attempt_at = now() + make_interval(secs => :lease_s), attempts = attempts + 1
    WHERE id IN (
        SELECT id FROM delivery_outbox
        WHERE next_attempt_at <= now() AND delivered_at IS NULL AND dead_at IS NULL
        ORDER BY next_attempt_at, created_at
        LIMIT :n
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, kind, recipient_ids, payload, attempts
    """
)


async def dispatch_pending(limit: int | None = None) -> DispatchStats:
    """The sweeper body: claim a batch under a lease, publish with no session held,
    then record the outcome. See the module docstring for the full three-step shape.
    """
    settings = get_settings()
    batch_limit = limit if limit is not None else settings.outbox_sweep_batch_limit
    session_factory = get_session_factory()

    claimed: list[dict] = []
    async with session_factory() as session:
        result = await session.execute(_CLAIM_SQL, {"lease_s": settings.outbox_lease_s, "n": batch_limit})
        rows = result.mappings().all()
        for row in rows:
            entry = {
                "id": row["id"],
                "kind": row["kind"],
                # str(...) here: asyncpg returns UUID[] rows as its own
                # asyncpg.pgproto.pgproto.UUID objects, not python uuid.UUID -- and
                # uuid.UUID(<that>) raises (it is not a str), so normalize to str
                # once, right where the raw row is read, same as every dispatcher's
                # already-str recipient ids.
                "recipient_ids": [str(rid) for rid in row["recipient_ids"]],
                "attempts": row["attempts"],
                "dead": False,
                "event": None,
                # sender_id lives OUTSIDE the event dict -- see the Dispatcher type
                # comment. The outbox payload column itself is server-side only and
                # may keep sender_id; the rebuilt wire event must not.
                "sender_id": row["payload"].get("sender_id"),
            }
            if row["kind"] == "message":
                message = await session.get(models.Message, uuid.UUID(row["payload"]["message_id"]))
                if message is None:
                    entry["dead"] = True
                else:
                    entry["event"] = {
                        "type": "message",
                        "conversation_id": row["payload"]["conversation_id"],
                        "message_id": row["payload"]["message_id"],
                        "sender_device_id": row["payload"]["sender_device_id"],
                        "ciphertext": base64.b64encode(message.ciphertext).decode("ascii"),
                        "created_at": row["payload"]["created_at"],
                    }
            elif row["kind"] == "poll":
                # No hydration needed: the payload already IS the full snapshot
                # polls.py's _prepare_broadcast built at enqueue time, unlike
                # 'message' which re-reads ciphertext from its source table.
                entry["event"] = row["payload"]
            else:
                # No dispatcher is wired for any other kind. Leave it exactly as the
                # claim left it rather than silently discarding it; nothing inserts
                # an unrecognized kind today.
                continue
            claimed.append(entry)
        await session.commit()

    # Publish with NO session held -- this is the whole point of the lease.
    outcomes: list[tuple[uuid.UUID, str, list[str] | None]] = []
    for entry in claimed:
        if entry["dead"]:
            outcomes.append((entry["id"], "dead", None))
            continue
        dispatcher = _dispatchers.get(entry["kind"])
        if dispatcher is None:
            continue
        recipients = [uuid.UUID(rid) for rid in entry["recipient_ids"]]
        try:
            async with asyncio.timeout(settings.outbox_dispatch_timeout_s):
                failed = await dispatcher(recipients, entry["event"], entry["sender_id"])
        except TimeoutError:
            failed = [str(rid) for rid in recipients]
        except Exception:
            failed = [str(rid) for rid in recipients]
        if not failed:
            outcomes.append((entry["id"], "delivered", None))
        elif entry["attempts"] >= settings.outbox_max_attempts:
            outcomes.append((entry["id"], "dead", failed))
        else:
            outcomes.append((entry["id"], "retry", failed))

    delivered = retried = dead = 0
    if outcomes:
        attempts_by_id = {entry["id"]: entry["attempts"] for entry in claimed}
        async with session_factory() as session:
            for row_id, decision, failed in outcomes:
                if decision == "delivered":
                    await session.execute(delete(models.DeliveryOutbox).where(models.DeliveryOutbox.id == row_id))
                    delivered += 1
                elif decision == "dead":
                    await session.execute(
                        update(models.DeliveryOutbox)
                        .where(models.DeliveryOutbox.id == row_id)
                        .values(
                            dead_at=datetime.now(timezone.utc),
                            last_error="attempts_exhausted" if failed is not None else "source_missing",
                        )
                    )
                    dead += 1
                    logger.warning("outbox row dead id=%s", row_id)
                else:
                    await session.execute(
                        update(models.DeliveryOutbox)
                        .where(models.DeliveryOutbox.id == row_id)
                        .values(
                            recipient_ids=[uuid.UUID(rid) for rid in failed],
                            next_attempt_at=datetime.now(timezone.utc)
                            + timedelta(seconds=_backoff_seconds(attempts_by_id[row_id])),
                            last_error="publish_failed",
                        )
                    )
                    retried += 1
            await session.commit()

    return DispatchStats(claimed=len(claimed), delivered=delivered, retried=retried, dead=dead)


async def queue_stats() -> tuple[int, float]:
    """(pending count, oldest pending age in seconds) -- unbounded, no `limit`."""
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) AS pending, "
                "EXTRACT(EPOCH FROM (now() - MIN(created_at))) AS oldest_age_seconds "
                "FROM delivery_outbox WHERE delivered_at IS NULL AND dead_at IS NULL"
            )
        )
        row = result.mappings().one()
    pending = int(row["pending"] or 0)
    oldest_age = row["oldest_age_seconds"]
    return pending, (float(oldest_age) if oldest_age is not None else 0.0)
