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
