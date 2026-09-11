"""Secretary: live polls -- create, vote, tally, close (board card c162).

READ THIS BEFORE ADDING A ROUTE HERE. Ballots are secret: no response in this
module reveals who voted for what, and `my_option_id` describes only the caller.
The tally is computed with GROUP BY, so a voter identity is never even loaded into
memory on the read path. If a future card genuinely needs a named vote, that is a
different feature with a different table -- do not widen these responses.

THAT RULE EXTENDS TO THE SOCKET. The broadcast payload carries the aggregate poll
and NOTHING about who voted -- deliberately not even `my_option_id`, which is
per-viewer and would be wrong in a message every member of the chapter receives.
Clients keep their own `my_option_id` across an update, which is correct because
only your own vote can change it.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import models
from app.core.analytics import emit
from app.core.errors import conflict, not_found
from app.core.permissions import POLLS_ADMIN, require_role
from app.core.rate_limits import enforce_limit
from app.db import get_session
from app.middleware.auth import get_verified_uid
from app.middleware.org_scope import get_current_membership
from app.schemas.polls import PollCreate, PollOptionResult, PollOut, PollVoteIn
from app.services import outbox
from app.ws.pubsub import publish_to_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["polls"])

# One aggregate update per vote makes a script expensive for the whole chapter.
# Thirty attempts per minute still allows repeated human changes of mind; every
# account/poll pair has its own shared Redis budget, with the existing fallback.
POLL_VOTE_LIMIT = (30, 60)
# A total chapter-delivery budget, not a fresh timeout for each recipient. Durable
# retries and aggregate coalescing are wired onto the c356 delivery outbox (board
# card c345); this deadline still bounds only this one best-effort live attempt --
# a row that misses it is left pending for the sweeper, not retried in-process.
POLL_BROADCAST_TIMEOUT_SECONDS = 1.0


async def _limit_poll_vote(
    poll_id: uuid.UUID, uid: str = Depends(get_verified_uid)
) -> None:
    """Throttle before membership lookup checks out a database connection."""
    await enforce_limit("poll_vote", f"{uid}:{poll_id}", POLL_VOTE_LIMIT)


@dataclass(frozen=True)
class _PollBroadcast:
    """Detached delivery inputs; publishing cannot accidentally lazy-load ORM data."""

    recipients: tuple[str, ...]
    event: dict[str, object]


async def _get_chapter_poll(
    session: AsyncSession, chapter_id: uuid.UUID, poll_id: uuid.UUID, *, lock: bool = False
) -> models.Poll:
    """Load a poll with its options, scoped to the path's chapter, or raise 404.

    Options are eager-loaded: this is an async session, so touching the lazy
    relationship later raises MissingGreenlet rather than emitting a query.
    """
    query = (
        select(models.Poll)
        .options(selectinload(models.Poll.options))
        .where(models.Poll.id == poll_id, models.Poll.chapter_id == chapter_id)
    )
    if lock:
        # All lifecycle writers take this lock before inspecting status or ballots.
        # The following option/tally queries therefore observe the preceding writer's
        # committed result, and first-vote/delete cannot both pass their guards.
        query = query.with_for_update(of=models.Poll).execution_options(populate_existing=True)
    poll = await session.scalar(query)
    if poll is None:
        raise not_found("poll_not_found")
    return poll


async def _tally(session: AsyncSession, poll_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Votes per option. GROUP BY, so no voter identity is loaded."""
    rows = await session.execute(
        select(models.PollVote.option_id, func.count())
        .where(models.PollVote.poll_id == poll_id)
        .group_by(models.PollVote.option_id)
    )
    return {option_id: count for option_id, count in rows}


async def _prepare_broadcast(
    session: AsyncSession,
    poll: models.Poll,
    action: str,
    counts: dict[uuid.UUID, int] | None,
) -> _PollBroadcast:
    """Snapshot aggregate content and active recipients before the transaction commits."""
    event: dict = {
        "type": "poll",
        "action": action,
        "chapter_id": str(poll.chapter_id),
        "poll_id": str(poll.id),
    }
    if counts is not None:
        options = [
            {
                "id": str(opt.id),
                "text": opt.text_,
                "position": opt.position,
                "votes": counts.get(opt.id, 0),
            }
            for opt in poll.options
        ]
        event["poll"] = {
            "id": str(poll.id),
            "chapter_id": str(poll.chapter_id),
            "meeting_id": str(poll.meeting_id) if poll.meeting_id is not None else None,
            "question": poll.question,
            "status": poll.status,
            "created_by": str(poll.created_by),
            "created_at": poll.created_at.isoformat(),
            "closed_at": poll.closed_at.isoformat() if poll.closed_at is not None else None,
            "options": options,
            "total_votes": sum(opt["votes"] for opt in options),
        }

    members = await session.execute(
        select(models.Membership.user_id).where(
            models.Membership.chapter_id == poll.chapter_id,
            models.Membership.status == "active",
        )
    )
    return _PollBroadcast(tuple(str(user_id) for user_id in members.scalars()), event)


async def _enqueue_poll_event(
    session: AsyncSession,
    poll_id: uuid.UUID,
    recipient_ids: list[uuid.UUID],
    event: dict[str, object],
) -> uuid.UUID:
    """Coalesce to at most one pending 'poll' outbox row per poll, then enqueue the
    latest snapshot in the caller's own (not yet committed) transaction.

    Deliberately NOT scoped to `next_attempt_at <= now()`, even though that looks
    like the natural match for "still pending, unleased" -- it is wrong for this
    predicate. `dispatch_now`'s own failure branch (app/services/outbox.py) pushes
    a row's `next_attempt_at` into the future SYNCHRONOUSLY, inside the same
    request/response cycle as the failed live dispatch; there is no live-path
    window that spans two separate HTTP requests. So by the time a SECOND vote's
    coalescing check runs here, a first vote's already-backed-off row would look
    "leased" under a next_attempt_at filter and survive uncoalesced -- two failed
    votes in a row would leave two pending rows instead of one, with the first
    (stale) row's superseded tally still queued for delivery. Delete every other
    still-open ('poll', not yet delivered, not dead) pending row for this poll_id
    unconditionally instead; a genuinely in-flight sweeper claim is safe against
    this race too (SKIP LOCKED avoids deadlock, and the sweeper has already
    captured its own in-memory event/recipient copy before its own commit, so a
    concurrent delete here just makes the sweeper's later outcome-write a
    harmless no-op against a missing row id).
    """
    await session.execute(
        delete(models.DeliveryOutbox).where(
            models.DeliveryOutbox.kind == "poll",
            models.DeliveryOutbox.payload["poll_id"].astext == str(poll_id),
            models.DeliveryOutbox.delivered_at.is_(None),
            models.DeliveryOutbox.dead_at.is_(None),
        )
    )
    return await outbox.enqueue(session, kind="poll", recipient_ids=recipient_ids, payload=event)


async def _broadcast(
    recipient_ids: list[uuid.UUID], event: dict[str, object], sender_id: str | None
) -> list[str]:
    """Deliver one snapshot with one deadline and no database session; return the
    recipient ids (as strings) that did not receive it.

    Registered below as the 'poll' outbox dispatcher (board c345) -- this is the
    literal module-level function `outbox.dispatch_now`/`dispatch_pending` call for
    kind='poll', and the one `test_c356_outbox_message_delivery.py`'s wiring pin
    checks by identity. `sender_id` is unused: polls never skip delivery to their
    own author (there is no per-recipient content-free push for polls to skip), it
    exists only because every Dispatcher must share this same signature.
    """
    started = asyncio.get_running_loop().time()
    failed: list[str] = []
    reached = 0
    timed_out = False
    try:
        async with asyncio.timeout(POLL_BROADCAST_TIMEOUT_SECONDS):
            for index, user_id in enumerate(recipient_ids):
                try:
                    await publish_to_user(str(user_id), event)
                except Exception:
                    # Do not log per recipient or include exception text: a failed
                    # client can carry credentials, and ballots must remain secret.
                    failed.append(str(user_id))
                reached = index + 1
    except TimeoutError:
        timed_out = True
    if timed_out:
        # Never reached the try/except for these -- count every untried recipient
        # as failed too, so the outbox actually retries them instead of the old
        # behavior of silently dropping an untried-on-timeout tail from both counts.
        failed.extend(str(user_id) for user_id in recipient_ids[reached:])
    if failed or timed_out:
        # JSON in the existing application log message. This is delivery telemetry,
        # not an assertion that Cloud Logging's formatter/sink has been reconfigured.
        logger.warning(json.dumps({
            "event": "poll_broadcast_incomplete",
            "poll_id": event["poll_id"],
            "chapter_id": event["chapter_id"],
            "action": event["action"],
            "recipients": len(recipient_ids),
            "delivered": len(recipient_ids) - len(failed),
            "failures": len(failed),
            "timed_out": timed_out,
            "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        }))
    return failed


outbox.register_dispatcher("poll", _broadcast)


def _assemble(
    poll: models.Poll,
    counts: dict[uuid.UUID, int],
    my_option_id: uuid.UUID | None,
) -> PollOut:
    options = [
        PollOptionResult(
            id=opt.id, text=opt.text_, position=opt.position, votes=counts.get(opt.id, 0)
        )
        for opt in poll.options
    ]
    return PollOut(
        id=poll.id,
        chapter_id=poll.chapter_id,
        meeting_id=poll.meeting_id,
        question=poll.question,
        status=poll.status,
        created_by=poll.created_by,
        created_at=poll.created_at,
        closed_at=poll.closed_at,
        options=options,
        # Summed from the per-option tallies rather than counted separately, so the
        # total can never disagree with the bars drawn above it.
        total_votes=sum(opt.votes for opt in options),
        my_option_id=my_option_id,
    )


@router.post("/chapters/{chapter_id}/polls", status_code=201)
async def create_poll(
    chapter_id: uuid.UUID,
    body: PollCreate,
    membership: models.Membership = Depends(require_role(*POLLS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> PollOut:
    """Open a poll; secretary/president only."""
    if body.meeting_id is not None:
        meeting = await session.get(models.Meeting, body.meeting_id)
        # Checked rather than left to the FK: attaching a poll to another chapter's
        # meeting must read as "no such meeting here", not as a 500.
        if meeting is None or meeting.chapter_id != chapter_id:
            raise not_found("meeting_not_found")

    poll = models.Poll(
        chapter_id=chapter_id,
        meeting_id=body.meeting_id,
        question=body.question,
        status="open",
        created_by=membership.user_id,
    )
    poll.options = [
        models.PollOption(text_=text, position=index)
        for index, text in enumerate(body.options)
    ]
    session.add(poll)
    await session.flush()
    poll = await _get_chapter_poll(session, chapter_id, poll.id)
    response = _assemble(poll, {}, None)
    broadcast = await _prepare_broadcast(session, poll, "opened", {})
    recipient_ids = [uuid.UUID(r) for r in broadcast.recipients]
    outbox_row_id = await _enqueue_poll_event(session, poll.id, recipient_ids, broadcast.event)
    await session.commit()
    try:
        # Best-effort immediate delivery. Any failure here -- including the follow-up
        # write inside dispatch_now itself -- leaves the outbox row exactly as
        # enqueued above, pending for the sweeper.
        await outbox.dispatch_now(
            session, outbox_row_id, kind="poll",
            recipient_ids=recipient_ids, event=broadcast.event, sender_id=None,
        )
    except Exception:
        # poll_id only -- never poll.id post-commit (see _enqueue_poll_event's
        # docstring on delete_poll's expired-instance hazard; the same call shape
        # is kept identical across all four sites rather than varying it per site).
        logger.warning("outbox live dispatch follow-up failed poll_id=%s", broadcast.event["poll_id"])
    return response


@router.get("/chapters/{chapter_id}/polls")
async def list_polls(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID | None = None,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[PollOut]:
    """List the chapter's polls, newest first; any member.

    Cursored on (created_at, id): polls grow with TIME - the docstring below already
    notes a chapter running one at every meeting "accumulates these forever" - so a cap
    alone would only move the truncation later (board c258). The three-queries-total
    property is preserved: the tallies and the caller's ballots are fetched once for the
    PAGE, which is the same shape at a bounded size.

    Three queries total regardless of how many polls come back -- the tallies and
    the caller's own ballots are each fetched once for the whole page. Doing it per
    poll would be the same numbers at N+1 the cost, and a chapter that runs a poll
    at every meeting accumulates these forever.
    """
    filters = [models.Poll.chapter_id == chapter_id]
    if meeting_id is not None:
        filters.append(models.Poll.meeting_id == meeting_id)

    if before is not None and before_id is not None:
        filters.append(
            tuple_(models.Poll.created_at, models.Poll.id) < (before, before_id)
        )
    elif before is not None:
        filters.append(models.Poll.created_at < before)

    result = await session.execute(
        select(models.Poll)
        .options(selectinload(models.Poll.options))
        .where(*filters)
        .order_by(models.Poll.created_at.desc(), models.Poll.id.desc())
        .limit(limit)
    )
    polls = list(result.scalars().all())
    if not polls:
        return []

    poll_ids = [poll.id for poll in polls]

    tally = await session.execute(
        select(models.PollVote.poll_id, models.PollVote.option_id, func.count())
        .where(models.PollVote.poll_id.in_(poll_ids))
        .group_by(models.PollVote.poll_id, models.PollVote.option_id)
    )
    counts: dict[uuid.UUID, dict[uuid.UUID, int]] = {}
    for row_poll_id, option_id, count in tally:
        counts.setdefault(row_poll_id, {})[option_id] = count

    mine_rows = await session.execute(
        select(models.PollVote.poll_id, models.PollVote.option_id).where(
            models.PollVote.poll_id.in_(poll_ids),
            models.PollVote.user_id == membership.user_id,
        )
    )
    mine = {row_poll_id: option_id for row_poll_id, option_id in mine_rows}

    return [_assemble(poll, counts.get(poll.id, {}), mine.get(poll.id)) for poll in polls]


async def _read_one(
    session: AsyncSession, poll: models.Poll, user_id: uuid.UUID
) -> PollOut:
    counts = await _tally(session, poll.id)
    my_option_id = await session.scalar(
        select(models.PollVote.option_id).where(
            models.PollVote.poll_id == poll.id, models.PollVote.user_id == user_id
        )
    )
    return _assemble(poll, counts, my_option_id)


@router.get("/chapters/{chapter_id}/polls/{poll_id}")
async def get_poll(
    chapter_id: uuid.UUID,
    poll_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> PollOut:
    """Read one poll with its current tally; any member."""
    poll = await _get_chapter_poll(session, chapter_id, poll_id)
    return await _read_one(session, poll, membership.user_id)


@router.post(
    "/chapters/{chapter_id}/polls/{poll_id}/vote",
    dependencies=[Depends(_limit_poll_vote)],
)
async def cast_vote(
    chapter_id: uuid.UUID,
    poll_id: uuid.UUID,
    body: PollVoteIn,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> PollOut:
    """Cast or change the caller's vote; any member of the chapter.

    Changing a vote is an UPDATE of the same row, not a second ballot -- the
    primary key (poll_id, user_id) makes that a database guarantee, so even two
    simultaneous requests cannot produce two votes from one member.
    """
    poll = await _get_chapter_poll(session, chapter_id, poll_id, lock=True)
    if poll.status != "open":
        raise conflict("poll_closed")
    if body.option_id not in {opt.id for opt in poll.options}:
        # The composite FK would also refuse this, but as an IntegrityError/500.
        raise not_found("option_not_found")

    await session.execute(
        pg_insert(models.PollVote)
        .values(poll_id=poll.id, user_id=membership.user_id, option_id=body.option_id)
        .on_conflict_do_update(
            index_elements=["poll_id", "user_id"],
            set_={"option_id": body.option_id},
        )
    )
    counts = await _tally(session, poll.id)
    response = _assemble(poll, counts, body.option_id)
    broadcast = await _prepare_broadcast(session, poll, "updated", counts)
    recipient_ids = [uuid.UUID(r) for r in broadcast.recipients]
    outbox_row_id = await _enqueue_poll_event(session, poll.id, recipient_ids, broadcast.event)
    await session.commit()
    # Board c227: SECRET BALLOT, same rule this file's module docstring already
    # states for every other response here - poll_id + a scope id, deliberately NO
    # user_id/voter id anywhere in this call. The card asked for "poll_id +
    # campus_id"; Poll has no campus_id column (chapter_id is what scopes it, see
    # app/models/polls.py), so chapter_id is what is actually emitted - already in
    # hand from the path, no extra query added purely for telemetry on every vote.
    emit("poll_voted", poll_id=poll_id, chapter_id=chapter_id)
    try:
        await outbox.dispatch_now(
            session, outbox_row_id, kind="poll",
            recipient_ids=recipient_ids, event=broadcast.event, sender_id=None,
        )
    except Exception:
        logger.warning("outbox live dispatch follow-up failed poll_id=%s", broadcast.event["poll_id"])
    return response


@router.post("/chapters/{chapter_id}/polls/{poll_id}/close")
async def close_poll(
    chapter_id: uuid.UUID,
    poll_id: uuid.UUID,
    membership: models.Membership = Depends(require_role(*POLLS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> PollOut:
    """Close a poll to further voting; secretary/president only.

    Closing an already-closed poll is a no-op returning the same body rather than a
    409: two officers tapping "close" on the same poll is ordinary, and the second
    one has not done anything wrong.
    """
    poll = await _get_chapter_poll(session, chapter_id, poll_id, lock=True)
    changed = poll.status == "open"
    if changed:
        poll.status = "closed"
        poll.closed_at = datetime.now(timezone.utc)
    response = await _read_one(session, poll, membership.user_id)
    broadcast = None
    recipient_ids: list[uuid.UUID] = []
    outbox_row_id: uuid.UUID | None = None
    if changed:
        broadcast = await _prepare_broadcast(
            session, poll, "updated", {option.id: option.votes for option in response.options}
        )
        recipient_ids = [uuid.UUID(r) for r in broadcast.recipients]
        outbox_row_id = await _enqueue_poll_event(session, poll.id, recipient_ids, broadcast.event)
    await session.commit()
    # Only the transition broadcasts/enqueues. A second officer tapping close must
    # not re-push an event that says nothing changed.
    if broadcast is not None:
        try:
            await outbox.dispatch_now(
                session, outbox_row_id, kind="poll",
                recipient_ids=recipient_ids, event=broadcast.event, sender_id=None,
            )
        except Exception:
            logger.warning("outbox live dispatch follow-up failed poll_id=%s", broadcast.event["poll_id"])
    return response


@router.delete("/chapters/{chapter_id}/polls/{poll_id}", status_code=204)
async def delete_poll(
    chapter_id: uuid.UUID,
    poll_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*POLLS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a poll NOBODY HAS VOTED ON; secretary/president only.

    c162 policy (Jose-delegated ruling, Aug 24): a poll with even one ballot is a
    record - deletion is forbidden with 409 poll_has_ballots, and CLOSE is the only
    lifecycle action left; results stay visible and ballots are immutable. A
    zero-ballot poll may still hard-delete: a mis-created question nobody answered
    is clutter, not history.
    """
    poll = await _get_chapter_poll(session, chapter_id, poll_id, lock=True)
    ballot_exists = await session.scalar(
        select(models.PollVote.poll_id)
        .where(models.PollVote.poll_id == poll.id)
        .limit(1)
    )
    if ballot_exists is not None:
        raise conflict("poll_has_ballots")
    broadcast = await _prepare_broadcast(session, poll, "deleted", None)
    recipient_ids = [uuid.UUID(r) for r in broadcast.recipients]
    # Enqueue BEFORE session.delete(poll): poll.id is still safe to read here, but
    # the ORM instance is expired-on-commit the moment session.delete(poll) commits,
    # and touching poll.id afterward would trigger an ObjectDeletedError refresh
    # attempt against a row that no longer exists -- this is why every dispatch_now
    # failure log below reads broadcast.event["poll_id"], never poll.id.
    outbox_row_id = await _enqueue_poll_event(session, poll.id, recipient_ids, broadcast.event)
    await session.delete(poll)
    await session.commit()
    try:
        await outbox.dispatch_now(
            session, outbox_row_id, kind="poll",
            recipient_ids=recipient_ids, event=broadcast.event, sender_id=None,
        )
    except Exception:
        logger.warning("outbox live dispatch follow-up failed poll_id=%s", broadcast.event["poll_id"])
