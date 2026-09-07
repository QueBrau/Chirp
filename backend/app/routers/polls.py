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
from sqlalchemy import func, select, tuple_
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
from app.ws.pubsub import publish_to_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["polls"])

# One aggregate update per vote makes a script expensive for the whole chapter.
# Thirty attempts per minute still allows repeated human changes of mind; every
# account/poll pair has its own shared Redis budget, with the existing fallback.
POLL_VOTE_LIMIT = (30, 60)
# A total chapter-delivery budget, not a fresh timeout for each recipient. Durable
# retries and aggregate coalescing belong to c356; these updates remain best effort.
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


async def _broadcast(batch: _PollBroadcast) -> None:
    """Deliver a committed snapshot with one deadline and no database session."""
    started = asyncio.get_running_loop().time()
    delivered = 0
    failures = 0
    timed_out = False
    try:
        async with asyncio.timeout(POLL_BROADCAST_TIMEOUT_SECONDS):
            for user_id in batch.recipients:
                try:
                    await publish_to_user(user_id, batch.event)
                    delivered += 1
                except Exception:
                    # Do not log per recipient or include exception text: a failed
                    # client can carry credentials, and ballots must remain secret.
                    failures += 1
    except TimeoutError:
        timed_out = True
    if failures or timed_out:
        # JSON in the existing application log message. This is delivery telemetry,
        # not an assertion that Cloud Logging's formatter/sink has been reconfigured.
        logger.warning(json.dumps({
            "event": "poll_broadcast_incomplete",
            "poll_id": batch.event["poll_id"],
            "chapter_id": batch.event["chapter_id"],
            "action": batch.event["action"],
            "recipients": len(batch.recipients),
            "delivered": delivered,
            "failures": failures,
            "timed_out": timed_out,
            "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        }))


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
    await session.commit()
    await _broadcast(broadcast)
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
    await session.commit()
    # Board c227: SECRET BALLOT, same rule this file's module docstring already
    # states for every other response here - poll_id + a scope id, deliberately NO
    # user_id/voter id anywhere in this call. The card asked for "poll_id +
    # campus_id"; Poll has no campus_id column (chapter_id is what scopes it, see
    # app/models/polls.py), so chapter_id is what is actually emitted - already in
    # hand from the path, no extra query added purely for telemetry on every vote.
    emit("poll_voted", poll_id=poll_id, chapter_id=chapter_id)
    await _broadcast(broadcast)
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
    if changed:
        broadcast = await _prepare_broadcast(
            session, poll, "updated", {option.id: option.votes for option in response.options}
        )
    await session.commit()
    # Only the transition broadcasts. A second officer tapping close must not
    # re-push an event that says nothing changed.
    if broadcast is not None:
        await _broadcast(broadcast)
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
    await session.delete(poll)
    await session.commit()
    await _broadcast(broadcast)
