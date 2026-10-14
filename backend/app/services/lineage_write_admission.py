"""Actor and target admission for lineage edge writes."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.permissions import EBOARD
from app.db import get_session, get_session_factory
from app.middleware.auth import get_current_user, get_verified_uid
from app.middleware.org_scope import get_current_membership
from app.services import lineage_service
from app.schemas.lineage import LineageEdgeCreate
from app.services.account_write_guard import (
    assert_account_write_targets,
    bind_account_write_guard,
)


async def _authorized_actor(
    session: AsyncSession, *, uid: str, chapter_id: uuid.UUID,
) -> tuple[models.User, models.Membership]:
    user = await get_current_user(uid=uid, session=session)
    membership = await get_current_membership(
        chapter_id=chapter_id, user=user, session=session
    )
    if membership.role not in {role.value for role in EBOARD}:
        raise HTTPException(status_code=403, detail="insufficient_role")
    return user, membership


async def _reject_tombstones(
    session: AsyncSession, *, user_ids: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Completed deletion tombstones are not reusable lineage placeholders."""
    result = await session.execute(
        select(models.User.id, models.User.firebase_uid)
        .where(models.User.id.in_(user_ids))
    )
    for row in result:
        if row.firebase_uid == f"deleted:{row.id}":
            raise HTTPException(status_code=422, detail="lineage_target_not_in_chapter")


async def get_lineage_edge_writer(
    chapter_id: uuid.UUID,
    body: LineageEdgeCreate,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.Membership:
    """Authorize first, then bind actor and both endpoints before request SQL."""
    target_ids = (body.big_user_id, body.little_user_id)
    settings = get_settings()
    if not settings.account_deletion_http_admission_enabled:
        _user, membership = await _authorized_actor(
            session, uid=uid, chapter_id=chapter_id
        )
        return membership

    # Do ordinary chapter/role and target-eligibility checks before binding the
    # request session. Unauthorized callers therefore cannot probe target holds.
    async with get_session_factory()() as preflight:
        actor, _membership = await _authorized_actor(
            preflight, uid=uid, chapter_id=chapter_id
        )
        if body.big_user_id == body.little_user_id:
            raise HTTPException(status_code=422, detail="big_and_little_identical")
        await lineage_service._validate_lineage_targets(
            preflight, chapter_id=chapter_id, user_ids=set(target_ids)
        )
        await _reject_tombstones(preflight, user_ids=target_ids)

    bind_account_write_guard(
        session,
        user_id=actor.id,
        firebase_uid=uid,
        target_user_ids=target_ids,
    )
    actor, membership = await _authorized_actor(
        session, uid=uid, chapter_id=chapter_id
    )
    current_target_ids = (body.big_user_id, body.little_user_id)
    assert_account_write_targets(
        session, user_id=actor.id, target_user_ids=current_target_ids
    )
    if body.big_user_id == body.little_user_id:
        raise HTTPException(status_code=422, detail="big_and_little_identical")
    await _reject_tombstones(session, user_ids=current_target_ids)
    await lineage_service._validate_lineage_targets(
        session, chapter_id=chapter_id, user_ids=set(current_target_ids)
    )
    current_target_ids = (body.big_user_id, body.little_user_id)
    assert_account_write_targets(
        session, user_id=actor.id, target_user_ids=current_target_ids
    )
    return membership
