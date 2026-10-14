"""Write admission helpers for feed routes that need ordinary auth preflight."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.campus_access import require_verified_campus
from app.core.errors import forbidden, not_found
from app.db import get_session, get_session_factory
from app.middleware.auth import get_current_user, get_verified_uid
from app.middleware.org_scope import get_current_membership
from app.services.account_write_guard import bind_account_write_guard


@dataclass(frozen=True)
class PostEditor:
    user: models.User
    membership: models.Membership


async def _validate_edit_scope(
    session: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    post_id: uuid.UUID,
    user: models.User,
    membership: models.Membership,
) -> models.Post:
    post = await session.scalar(
        select(models.Post).where(models.Post.id == post_id)
    )
    if post is None or post.chapter_id != chapter_id or post.deleted_at is not None:
        raise not_found("post_not_found")
    if post.author_id != membership.user_id:
        raise forbidden("not_author")
    if post.audience == "campus":
        require_verified_campus(user, post.campus_id)
    return post


async def get_post_editor_for_write(
    chapter_id: uuid.UUID,
    post_id: uuid.UUID,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> PostEditor:
    """Authorize post ownership before binding a pristine request session."""
    if not get_settings().account_deletion_http_admission_enabled:
        user = await get_current_user(uid=uid, session=session)
        membership = await get_current_membership(
            chapter_id=chapter_id, user=user, session=session
        )
        return PostEditor(user=user, membership=membership)

    # Preserve the existing 401/403/404 and campus-check ordering before asking the
    # hold guard to admit this author-owned mutation.
    async with get_session_factory()() as preflight:
        user = await get_current_user(uid=uid, session=preflight)
        membership = await get_current_membership(
            chapter_id=chapter_id, user=user, session=preflight
        )
        post = await _validate_edit_scope(
            preflight,
            chapter_id=chapter_id,
            post_id=post_id,
            user=user,
            membership=membership,
        )

    bind_account_write_guard(
        session,
        user_id=user.id,
        firebase_uid=uid,
        target_user_ids=(post.author_id,),
    )
    current = await get_current_user(uid=uid, session=session)
    current_membership = await get_current_membership(
        chapter_id=chapter_id, user=current, session=session
    )
    return PostEditor(user=current, membership=current_membership)
