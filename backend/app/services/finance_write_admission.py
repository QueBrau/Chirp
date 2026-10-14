"""Target-aware account admission for dues-plan creation."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.permissions import DUES_ADMIN
from app.db import get_session, get_session_factory
from app.middleware.auth import get_current_user, get_verified_uid
from app.middleware.org_scope import get_current_membership
from app.schemas.finance import DuesPaymentPlanCreate
from app.services.account_write_guard import (
    assert_account_write_targets,
    bind_account_write_guard,
)


def _require_dues_admin(membership: models.Membership) -> None:
    if membership.role not in {role.value for role in DUES_ADMIN}:
        raise HTTPException(status_code=403, detail="insufficient_role")


async def _authorize_cycle_and_target(
    session: AsyncSession,
    *,
    actor: models.User,
    chapter_id: uuid.UUID,
    cycle_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> models.Membership:
    membership = await get_current_membership(
        chapter_id=chapter_id, user=actor, session=session
    )
    _require_dues_admin(membership)

    cycle = await session.get(models.DuesCycle, cycle_id)
    if cycle is None or cycle.chapter_id != chapter_id:
        raise HTTPException(status_code=404, detail="dues_cycle_not_found")

    target_membership = await session.scalar(
        select(models.Membership.id).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == target_user_id,
            models.Membership.status == "active",
        )
    )
    if target_membership is None:
        # Keep nonexistent and non-member targets indistinguishable.
        raise HTTPException(status_code=404, detail="membership_not_found")

    target = await session.scalar(
        select(models.User.firebase_uid).where(models.User.id == target_user_id)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="membership_not_found")
    if target == f"deleted:{target_user_id}":
        raise HTTPException(status_code=422, detail="dues_plan_target_deleted")
    return membership


async def get_dues_plan_writer(
    chapter_id: uuid.UUID,
    cycle_id: uuid.UUID,
    body: DuesPaymentPlanCreate,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.Membership:
    """Authorize ordinary finance scope before binding actor and plan target."""
    target_ids = (body.user_id,)
    if not get_settings().account_deletion_http_admission_enabled:
        actor = await get_current_user(uid=uid, session=session)
        membership = await get_current_membership(
            chapter_id=chapter_id, user=actor, session=session
        )
        _require_dues_admin(membership)
        return membership

    # The first pass uses a separate session so role and ordinary target eligibility
    # are established before the deletion hold can be observed.
    async with get_session_factory()() as preflight:
        actor = await get_current_user(uid=uid, session=preflight)
        await _authorize_cycle_and_target(
            preflight,
            actor=actor,
            chapter_id=chapter_id,
            cycle_id=cycle_id,
            target_user_id=body.user_id,
        )

    bind_account_write_guard(
        session,
        user_id=actor.id,
        firebase_uid=uid,
        target_user_ids=target_ids,
    )
    actor = await get_current_user(uid=uid, session=session)
    assert_account_write_targets(
        session, user_id=actor.id, target_user_ids=(body.user_id,)
    )
    membership = await _authorize_cycle_and_target(
        session,
        actor=actor,
        chapter_id=chapter_id,
        cycle_id=cycle_id,
        target_user_id=body.user_id,
    )
    return membership


def assert_dues_plan_target_scope(
    session: AsyncSession, *, actor_user_id: uuid.UUID, target_user_id: uuid.UUID,
) -> None:
    """Fail closed if a request body changes after its target scope was bound."""
    if get_settings().account_deletion_http_admission_enabled:
        assert_account_write_targets(
            session, user_id=actor_user_id, target_user_ids=(target_user_id,)
        )
