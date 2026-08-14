"""Moderation: content reports, user blocks, and admin yak removal."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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

    This only proves "is a moderator somewhere" — it is NOT sufficient authorization
    on its own. Callers MUST additionally scope to the specific campus/chapter of the
    target (see list_reports, remove_yak) so an e-board member of chapter A cannot see
    or act on chapter B's reports/yaks (SECURITY-REVIEW finding 1). Gating chapter
    creation itself (self-serve presidency) is a separate product decision, carded
    on the board — out of scope here.
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


async def _resolve_report_campus_id(
    session: AsyncSession,
    target_type: str,
    target_id: uuid.UUID | None,
    reporter: models.User,
) -> uuid.UUID | None:
    """Resolve the campus a report belongs to, server-side (SECURITY-REVIEW finding 1).

    yak -> yak.campus_id; post/comment -> the post's chapter -> chapter.campus_id;
    anything else (message_forward, user, or a missing/unresolvable target) falls back
    to the reporter's own users.campus_id, best-effort. Never trusts the client.
    """
    if target_id is not None and target_type == "yak":
        yak = await session.get(models.Yak, target_id)
        if yak is not None:
            return yak.campus_id
    elif target_id is not None and target_type == "post":
        post = await session.get(models.Post, target_id)
        if post is not None:
            chapter = await session.get(models.Chapter, post.chapter_id)
            if chapter is not None:
                return chapter.campus_id
    elif target_id is not None and target_type == "comment":
        comment = await session.get(models.PostComment, target_id)
        if comment is not None:
            post = await session.get(models.Post, comment.post_id)
            if post is not None:
                chapter = await session.get(models.Chapter, post.chapter_id)
                if chapter is not None:
                    return chapter.campus_id
    return reporter.campus_id


@router.post("/moderation/reports", status_code=201)
async def create_report(
    body: ContentReportCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ContentReportOut:
    """File a content report; forwarded_plaintext supports E2EE message reports (SPEC §6.7).

    campus_id is resolved server-side from the target (SECURITY-REVIEW finding 1) so
    list_reports/remove_yak can scope moderation to the right campus.
    """
    campus_id = await _resolve_report_campus_id(session, body.target_type, body.target_id, user)
    report = models.ContentReport(
        reporter_id=user.id,
        campus_id=campus_id,
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
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> list[ContentReportOut]:
    """List reports newest first, scoped to campuses where the caller is active e-board.

    SECURITY-REVIEW finding 1: previously returned every report platform-wide
    (including forwarded_plaintext of reported E2EE messages) to any e-board member
    of any chapter. Now restricted to reports whose campus_id is one the caller
    actually moderates.
    """
    campus_ids_result = await session.execute(
        select(models.Chapter.campus_id)
        .join(models.Membership, models.Membership.chapter_id == models.Chapter.id)
        .where(
            models.Membership.user_id == moderator.id,
            models.Membership.status == "active",
            models.Membership.role.in_(_EBOARD_ROLES),
        )
        .distinct()
    )
    campus_ids = list(campus_ids_result.scalars().all())
    if not campus_ids:
        return []
    result = await session.execute(
        select(models.ContentReport)
        .where(models.ContentReport.campus_id.in_(campus_ids))
        .order_by(models.ContentReport.created_at.desc())
    )
    return [ContentReportOut.model_validate(r) for r in result.scalars().all()]


@router.post("/moderation/blocks", status_code=201)
async def create_block(
    body: UserBlockCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserBlockOut:
    """Block another user; 409 if the block already exists (incl. a concurrent double-tap)."""
    existing = await session.get(models.UserBlock, (user.id, body.blocked_id))
    if existing is not None:
        raise conflict("already_blocked")
    block = models.UserBlock(blocker_id=user.id, blocked_id=body.blocked_id)
    session.add(block)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate insert race on the (blocker_id, blocked_id) PK.
        await session.rollback()
        raise conflict("already_blocked") from None
    await session.refresh(block)
    return UserBlockOut.model_validate(block)


@router.post("/moderation/blocks/by-yak/{yak_id}", status_code=204)
async def block_yak_author(
    yak_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Block the (anonymous) author of a yak without ever revealing who that author is.

    Resolves author_id server-side and returns no body — unlike POST /moderation/blocks,
    which echoes UserBlockOut(blocked_id=...). Echoing blocked_id here would let a caller
    block-by-yak on two different yaks and diff the response to learn whether they share
    an author, breaking the SPEC §8.3 anonymity invariant. For the same reason, an
    already-existing block (including one lost to a concurrent insert race) is treated
    as success (204) rather than 409: a 409-vs-204 split is itself a one-bit oracle for
    "have I already blocked this yak's author", which also leaks author identity across
    yaks. So this endpoint is idempotent by design, not just by convenience.
    """
    yak = await session.get(models.Yak, yak_id)
    if yak is None or yak.removed_at is not None:
        raise not_found("yak_not_found")
    if user.campus_id != yak.campus_id:
        raise forbidden("not_your_campus")
    if yak.author_id == user.id:
        raise forbidden("cannot_block_self")

    existing = await session.get(models.UserBlock, (user.id, yak.author_id))
    if existing is not None:
        return None
    block = models.UserBlock(blocker_id=user.id, blocked_id=yak.author_id)
    session.add(block)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate insert race on the (blocker_id, blocked_id) PK — same
        # idempotent-success reasoning as above, not a 409.
        await session.rollback()
    return None


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
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Admin removal: sets removed_at + removed_reason.

    SECURITY-REVIEW finding 1: previously any e-board member of any chapter could
    remove any campus's yaks. Now requires the caller to be active e-board in some
    chapter whose campus matches the yak's campus.
    """
    yak = await session.get(models.Yak, yak_id)
    if yak is None:
        raise not_found("yak_not_found")
    if yak.removed_at is not None:
        raise conflict("already_removed")
    campus_match = await session.execute(
        select(models.Membership.id)
        .join(models.Chapter, models.Chapter.id == models.Membership.chapter_id)
        .where(
            models.Membership.user_id == moderator.id,
            models.Membership.status == "active",
            models.Membership.role.in_(_EBOARD_ROLES),
            models.Chapter.campus_id == yak.campus_id,
        )
        .limit(1)
    )
    if campus_match.scalar_one_or_none() is None:
        raise forbidden("insufficient_role")
    yak.removed_at = datetime.now(timezone.utc)
    yak.removed_reason = body.reason
    await session.commit()
