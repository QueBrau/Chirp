"""Yak anonymous campus board: list/post yaks, vote, author delete (no author exposure, §8.3)."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.yak import YakCreate, YakOut, YakVoteCreate, YakVoteOut

router = APIRouter(tags=["yaks"])


class YakFeedOut(YakOut):
    """YakOut plus the caller's OWN vote only — still no author field of any kind (§8.3)."""

    my_vote: int | None = None


async def _require_campus_user(
    campus_id: uuid.UUID = Path(...),
    user: models.User = Depends(get_current_user),
) -> models.User:
    """403 unless the caller belongs to the campus in the path (users.campus_id)."""
    if user.campus_id != campus_id:
        raise forbidden("not_your_campus")
    return user


@router.get("/campuses/{campus_id}/yaks")
async def list_yaks(
    campus_id: uuid.UUID,
    user: models.User = Depends(_require_campus_user),
    session: AsyncSession = Depends(get_session),
) -> list[YakFeedOut]:
    """List the campus's yaks newest first (not removed), with score and the caller's own vote."""
    result = await session.execute(
        select(models.Yak, models.YakVote.value)
        .outerjoin(
            models.YakVote,
            (models.YakVote.yak_id == models.Yak.id)
            & (models.YakVote.user_id == user.id),
        )
        .where(
            models.Yak.campus_id == campus_id,
            models.Yak.removed_at.is_(None),
        )
        .order_by(models.Yak.created_at.desc())
    )
    items: list[YakFeedOut] = []
    for yak, my_vote in result.all():
        item = YakFeedOut.model_validate(yak)
        item.my_vote = my_vote
        items.append(item)
    return items


@router.post("/campuses/{campus_id}/yaks", status_code=201)
async def create_yak(
    campus_id: uuid.UUID,
    body: YakCreate,
    user: models.User = Depends(_require_campus_user),
    session: AsyncSession = Depends(get_session),
) -> YakOut:
    """Post an anonymous yak; author_id is stored server-side only, never returned."""
    yak = models.Yak(campus_id=campus_id, author_id=user.id, body=body.body)
    session.add(yak)
    await session.commit()
    await session.refresh(yak)
    return YakOut.model_validate(yak)


@router.put("/yaks/{yak_id}/vote")
async def vote_yak(
    yak_id: uuid.UUID,
    body: YakVoteCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> YakVoteOut:
    """Upsert the caller's -1/+1 vote and recompute the yak's score."""
    yak = await session.get(models.Yak, yak_id)
    if yak is None or yak.removed_at is not None:
        raise not_found("yak_not_found")
    if user.campus_id != yak.campus_id:
        raise forbidden("not_your_campus")
    vote = await session.get(models.YakVote, (yak_id, user.id))
    if vote is None:
        session.add(models.YakVote(yak_id=yak_id, user_id=user.id, value=body.value))
    else:
        vote.value = body.value
    await session.flush()
    await session.execute(
        update(models.Yak)
        .where(models.Yak.id == yak_id)
        .values(
            score=select(func.coalesce(func.sum(models.YakVote.value), 0))
            .where(models.YakVote.yak_id == yak_id)
            .scalar_subquery()
        )
    )
    await session.commit()
    return YakVoteOut(yak_id=yak_id, value=body.value)


@router.delete("/yaks/{yak_id}", status_code=204)
async def delete_yak(
    yak_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Author-only soft removal: sets removed_at with removed_reason='author_deleted'."""
    yak = await session.get(models.Yak, yak_id)
    if yak is None or yak.removed_at is not None:
        raise not_found("yak_not_found")
    if yak.author_id != user.id:
        raise forbidden("not_author")
    yak.removed_at = datetime.now(timezone.utc)
    yak.removed_reason = "author_deleted"
    await session.commit()
