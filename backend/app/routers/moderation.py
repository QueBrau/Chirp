"""Moderation: content reports, user blocks, and admin yak removal."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import conflict, forbidden, not_found
from app.core.permissions import EBOARD
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.yak import (
    ContentReportCreate,
    ContentReportOut,
    UserBlockCreate,
    UserBlockOut,
    YakRemoveRequest,
)

router = APIRouter(tags=["moderation"])

_EBOARD_ROLES: list[str] = [role.value for role in EBOARD]


async def _require_any_eboard(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """v1 scaffolding simplification: moderator = e-board member of ANY active chapter.

    There is no dedicated moderator role yet; a proper per-campus/per-target
    moderation model is future work. Documented here per the scaffold brief.
    """
    result = await session.execute(
        select(models.Membership.id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
            models.Membership.role.in_(_EBOARD_ROLES),
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise forbidden("insufficient_role")
    return user


@router.post("/moderation/reports", status_code=201)
async def create_report(
    body: ContentReportCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ContentReportOut:
    """File a content report; forwarded_plaintext supports E2EE message reports (SPEC §6.7)."""
    report = models.ContentReport(
        reporter_id=user.id,
        target_type=body.target_type,
        target_id=body.target_id,
        forwarded_plaintext=body.forwarded_plaintext,
        reason=body.reason,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return ContentReportOut.model_validate(report)


@router.get("/moderation/reports")
async def list_reports(
    _moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> list[ContentReportOut]:
    """List reports newest first; e-board of at least one active chapter (v1 simplification)."""
    result = await session.execute(
        select(models.ContentReport).order_by(models.ContentReport.created_at.desc())
    )
    return [ContentReportOut.model_validate(r) for r in result.scalars().all()]


@router.post("/moderation/blocks", status_code=201)
async def create_block(
    body: UserBlockCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserBlockOut:
    """Block another user; 409 if the block already exists."""
    existing = await session.get(models.UserBlock, (user.id, body.blocked_id))
    if existing is not None:
        raise conflict("already_blocked")
    block = models.UserBlock(blocker_id=user.id, blocked_id=body.blocked_id)
    session.add(block)
    await session.commit()
    await session.refresh(block)
    return UserBlockOut.model_validate(block)


@router.delete("/moderation/blocks", status_code=204)
async def delete_block(
    blocked_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Unblock a user (identified by ?blocked_id=); 404 if no such block."""
    block = await session.get(models.UserBlock, (user.id, blocked_id))
    if block is None:
        raise not_found("block_not_found")
    await session.delete(block)
    await session.commit()


@router.post("/moderation/yaks/{yak_id}/remove", status_code=204)
async def remove_yak(
    yak_id: uuid.UUID,
    body: YakRemoveRequest,
    _moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Admin removal: sets removed_at + removed_reason (same v1 e-board guard as reports)."""
    yak = await session.get(models.Yak, yak_id)
    if yak is None:
        raise not_found("yak_not_found")
    if yak.removed_at is not None:
        raise conflict("already_removed")
    yak.removed_at = datetime.now(timezone.utc)
    yak.removed_reason = body.reason
    await session.commit()
