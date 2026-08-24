"""Secretary: live polls -- create, vote, tally, close (board card c162).

READ THIS BEFORE ADDING A ROUTE HERE. Ballots are secret: no response in this
module reveals who voted for what, and `my_option_id` describes only the caller.
The tally is computed with GROUP BY, so a voter identity is never even loaded into
memory on the read path. If a future card genuinely needs a named vote, that is a
different feature with a different table -- do not widen these responses.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import models
from app.core.errors import conflict, not_found
from app.core.permissions import POLLS_ADMIN, require_role
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.polls import PollCreate, PollOptionResult, PollOut, PollVoteIn

router = APIRouter(tags=["polls"])


async def _get_chapter_poll(
    session: AsyncSession, chapter_id: uuid.UUID, poll_id: uuid.UUID
) -> models.Poll:
    """Load a poll with its options, scoped to the path's chapter, or raise 404.

    Options are eager-loaded: this is an async session, so touching the lazy
    relationship later raises MissingGreenlet rather than emitting a query.
    """
    poll = await session.scalar(
        select(models.Poll)
        .options(selectinload(models.Poll.options))
        .where(models.Poll.id == poll_id)
    )
    if poll is None or poll.chapter_id != chapter_id:
        raise not_found("poll_not_found")
    return poll


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
    await session.commit()
    poll = await _get_chapter_poll(session, chapter_id, poll.id)
    return _assemble(poll, {}, None)


@router.get("/chapters/{chapter_id}/polls")
async def list_polls(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID | None = None,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[PollOut]:
    """List the chapter's polls, newest first; any member.

    Three queries total regardless of how many polls come back -- the tallies and
    the caller's own ballots are each fetched once for the whole page. Doing it per
    poll would be the same numbers at N+1 the cost, and a chapter that runs a poll
    at every meeting accumulates these forever.
    """
    filters = [models.Poll.chapter_id == chapter_id]
    if meeting_id is not None:
        filters.append(models.Poll.meeting_id == meeting_id)

    result = await session.execute(
        select(models.Poll)
        .options(selectinload(models.Poll.options))
        .where(*filters)
        .order_by(models.Poll.created_at.desc())
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
    tally = await session.execute(
        select(models.PollVote.option_id, func.count())
        .where(models.PollVote.poll_id == poll.id)
        .group_by(models.PollVote.option_id)
    )
    counts = {option_id: count for option_id, count in tally}
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


@router.post("/chapters/{chapter_id}/polls/{poll_id}/vote")
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
    poll = await _get_chapter_poll(session, chapter_id, poll_id)
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
    await session.commit()
    return await _read_one(session, poll, membership.user_id)


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
    poll = await _get_chapter_poll(session, chapter_id, poll_id)
    if poll.status == "open":
        poll.status = "closed"
        poll.closed_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(poll)
    return await _read_one(session, poll, membership.user_id)


@router.delete("/chapters/{chapter_id}/polls/{poll_id}", status_code=204)
async def delete_poll(
    chapter_id: uuid.UUID,
    poll_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*POLLS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a poll and its ballots; secretary/president only."""
    poll = await _get_chapter_poll(session, chapter_id, poll_id)
    await session.delete(poll)
    await session.commit()
