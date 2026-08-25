"""Chirp anonymous campus board: list/post chirps, vote, author delete (no author exposure, §8.3)."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.campus_access import require_campus_member, require_verified_campus
from app.core.errors import forbidden, not_found
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.chirp import ChirpCreate, ChirpOut, ChirpVoteCreate, ChirpVoteOut

router = APIRouter(tags=["chirps"])


class ChirpFeedOut(ChirpOut):
    """ChirpOut plus the caller's OWN vote only — still no author field of any kind (§8.3)."""

    my_vote: int | None = None


@router.get("/campuses/{campus_id}/chirps")
async def list_chirps(
    campus_id: uuid.UUID,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> list[ChirpFeedOut]:
    """List the campus's chirps newest first (not removed), with score and the caller's own vote.

    Board card c127 (security hardening item 5): this had no limit at all, so a
    campus with enough history returned its entire chirp table on every load.
    Reverse-chron with a compound (created_at, id) cursor (`before` + `before_id`),
    same shape as feed.py's campus feed and messages.py's list_messages — matching
    the house pattern (see routers/feed.py:list_campus_feed and
    tests/test_pagination.py) rather than inventing a third one. `before` alone
    still works (legacy clients) but does not guarantee the tie-break. Backed by
    the existing idx_chirps_campus_time index, so this does not add a new query
    shape the schema wasn't already built for.

    Chirps whose (anonymous) author the caller has blocked are silently absent — no
    tombstone, no count — matching every other blocked chirp simply not existing for
    this caller (§8.3: nothing in the response may reveal that anything was hidden).
    """
    stmt = (
        select(models.Chirp, models.ChirpVote.value)
        .outerjoin(
            models.ChirpVote,
            (models.ChirpVote.chirp_id == models.Chirp.id)
            & (models.ChirpVote.user_id == user.id),
        )
        .outerjoin(
            models.UserBlock,
            (models.UserBlock.blocked_id == models.Chirp.author_id)
            & (models.UserBlock.blocker_id == user.id),
        )
        .where(
            models.Chirp.campus_id == campus_id,
            models.Chirp.removed_at.is_(None),
            models.UserBlock.blocker_id.is_(None),
        )
    )
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Chirp.created_at, models.Chirp.id) < (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Chirp.created_at < before)
    stmt = stmt.order_by(
        models.Chirp.created_at.desc(), models.Chirp.id.desc()
    ).limit(limit)

    result = await session.execute(stmt)
    items: list[ChirpFeedOut] = []
    for chirp, my_vote in result.all():
        item = ChirpFeedOut.model_validate(chirp)
        item.my_vote = my_vote
        items.append(item)
    return items


@router.post("/campuses/{campus_id}/chirps", status_code=201)
async def create_chirp(
    campus_id: uuid.UUID,
    body: ChirpCreate,
    user: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> ChirpOut:
    """Post an anonymous chirp; author_id is stored server-side only, never returned."""
    chirp = models.Chirp(campus_id=campus_id, author_id=user.id, body=body.body)
    session.add(chirp)
    await session.commit()
    await session.refresh(chirp)
    return ChirpOut.model_validate(chirp)


@router.put("/chirps/{chirp_id}/vote")
async def vote_chirp(
    chirp_id: uuid.UUID,
    body: ChirpVoteCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChirpVoteOut:
    """Upsert the caller's -1/+1 vote and recompute the chirp's score (idempotent, 200)."""
    chirp = await session.get(models.Chirp, chirp_id)
    if chirp is None or chirp.removed_at is not None:
        raise not_found("chirp_not_found")
    # No campus_id in this path, so the campus comes off the chirp and the shared check is
    # called directly. Voting is participation in a campus board, not a private action:
    # an unverified caller must be refused here exactly as they are on list and create,
    # or the gate is a front door with an open window beside it.
    require_verified_campus(user, chirp.campus_id)
    vote = await session.get(models.ChirpVote, (chirp_id, user.id))
    if vote is None:
        session.add(models.ChirpVote(chirp_id=chirp_id, user_id=user.id, value=body.value))
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent double-tap raced us to insert the same (chirp_id, user_id)
            # vote — rollback and fall through to an UPDATE instead of a 500.
            await session.rollback()
            vote = await session.get(models.ChirpVote, (chirp_id, user.id))
            if vote is None:
                raise
            vote.value = body.value
            await session.flush()
    else:
        vote.value = body.value
        await session.flush()
    await session.execute(
        update(models.Chirp)
        .where(models.Chirp.id == chirp_id)
        .values(
            score=select(func.coalesce(func.sum(models.ChirpVote.value), 0))
            .where(models.ChirpVote.chirp_id == chirp_id)
            .scalar_subquery()
        )
    )
    await session.commit()
    return ChirpVoteOut(chirp_id=chirp_id, value=body.value)


@router.delete("/chirps/{chirp_id}", status_code=204)
async def delete_chirp(
    chirp_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Author-only soft removal: sets removed_at with removed_reason='author_deleted'.

    DELIBERATELY NOT campus-gated, so this is not an omission — an author may always
    retract their own post. Gating deletion on a current verification would trap
    content: a student whose yearly re-check lapsed could no longer take down
    something they wrote, which is the opposite of what the gate is for. Authorship is
    already the stricter check here, and it does not depend on campus at all.
    """
    chirp = await session.get(models.Chirp, chirp_id)
    if chirp is None or chirp.removed_at is not None:
        raise not_found("chirp_not_found")
    if chirp.author_id != user.id:
        raise forbidden("not_author")
    chirp.removed_at = datetime.now(timezone.utc)
    chirp.removed_reason = "author_deleted"
    await session.commit()
