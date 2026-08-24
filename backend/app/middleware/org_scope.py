"""Org scoping: resolves (user, chapter_id) to an active membership before handlers run."""
import uuid

from fastapi import Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.db import get_session
from app.middleware.auth import get_current_user


async def get_current_membership(
    chapter_id: uuid.UUID = Path(...),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> models.Membership:
    """Return the caller's active membership in the path's chapter, or raise 403."""
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="not_a_member")
    return membership


async def get_current_chapter_member(
    chapter_id: uuid.UUID = Path(...),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> models.Membership:
    """Return the caller's membership in the path's chapter, active OR inactive - or
    raise 403. 'removed' does not count; a removed membership is not a member.

    Deliberately distinct from get_current_membership (active-only) above (board
    c102): the chapter-public feed tier is visible to any non-removed member, while
    the actives-only tier layered on top of it is filtered INSIDE the feed query by
    this same membership row's own status=='active', not by refusing entry here.
    Routes gating actual participation (posting, RSVPs, dues, moderation, ...)
    should keep using get_current_membership - this is for the feed READ path only.
    """
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == user.id,
            models.Membership.status.in_(("active", "inactive")),
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="not_a_member")
    return membership
