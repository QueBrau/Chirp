"""Chapter CRUD, member management, invite creation, and invite-code join."""
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import conflict, forbidden, not_found
from app.core.permissions import EBOARD, Role, require_role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.identity import (
    ChapterCreate,
    ChapterInviteCreate,
    ChapterInviteOut,
    ChapterJoinRequest,
    ChapterOut,
    MembershipOut,
    MembershipUpdate,
)

router = APIRouter(tags=["chapters"])

_EBOARD_ROLE_VALUES: frozenset[str] = frozenset(role.value for role in EBOARD)


@router.post("/chapters", status_code=201)
async def create_chapter(
    body: ChapterCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChapterOut:
    """Create a chapter; the creator becomes its president via a new membership."""
    chapter = models.Chapter(
        campus_id=body.campus_id,
        org_name=body.org_name,
        chapter_name=body.chapter_name,
    )
    session.add(chapter)
    await session.flush()
    session.add(
        models.Membership(
            user_id=user.id,
            chapter_id=chapter.id,
            role=Role.president.value,
        )
    )
    await session.commit()
    await session.refresh(chapter)
    return ChapterOut.model_validate(chapter)


@router.get("/chapters/{chapter_id}")
async def get_chapter(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> ChapterOut:
    """Return the chapter; org-scoped to active members (§8.4)."""
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    return ChapterOut.model_validate(chapter)


@router.get("/chapters/{chapter_id}/members")
async def list_members(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[MembershipOut]:
    """List the chapter's memberships with each member's display name; §8.4 org-scoped.

    The name is joined in because MembershipOut otherwise carries only user_id, and
    there is no GET /users/{id} to resolve it — a roster of bare UUIDs is unusable for
    the secretary's attendance view and the treasurer's spend-approval requesters.
    """
    result = await session.execute(
        select(models.Membership, models.User.display_name)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(models.Membership.chapter_id == chapter_id)
        .order_by(models.Membership.joined_at)
    )
    members: list[MembershipOut] = []
    for membership, display_name in result.all():
        item = MembershipOut.model_validate(membership)
        item.display_name = display_name
        members.append(item)
    return members


@router.patch("/chapters/{chapter_id}/members")
async def update_member(
    chapter_id: uuid.UUID,
    body: MembershipUpdate,
    _actor: models.Membership = Depends(require_role(Role.president)),
    session: AsyncSession = Depends(get_session),
) -> MembershipOut:
    """Update a member's role/status/pledge_class; president only."""
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == body.user_id,
        )
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise not_found("membership_not_found")
    if body.role is not None:
        target.role = body.role
    if body.status is not None:
        target.status = body.status
    if body.pledge_class is not None:
        target.pledge_class = body.pledge_class
    await session.commit()
    return MembershipOut.model_validate(target)


@router.post("/chapters/{chapter_id}/invites", status_code=201)
async def create_invite(
    chapter_id: uuid.UUID,
    body: ChapterInviteCreate,
    actor: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> ChapterInviteOut:
    """Create a deep-link invite code; e-board only. Optional expiry.

    SECURITY-REVIEW finding 2: minting an EBOARD-role invite (e.g. a historian
    inviting a future president) requires the creator to already be president —
    any e-board role may still mint non-eboard invites (member/pledge/alumni).
    """
    if body.role in _EBOARD_ROLE_VALUES and actor.role != Role.president.value:
        raise forbidden("insufficient_role")
    invite = models.ChapterInvite(
        chapter_id=chapter_id,
        code=secrets.token_urlsafe(9),
        role=body.role,
        expires_at=body.expires_at,
        created_by=actor.user_id,
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return ChapterInviteOut.model_validate(invite)


@router.post("/chapters/join", status_code=201)
async def join_chapter(
    body: ChapterJoinRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MembershipOut:
    """Redeem an invite code: validates expiry, 409 if already a member."""
    result = await session.execute(
        select(models.ChapterInvite).where(models.ChapterInvite.code == body.code)
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise not_found("invite_not_found")
    if invite.expires_at is not None and invite.expires_at <= datetime.now(timezone.utc):
        raise forbidden("invite_expired")
    existing = await session.execute(
        select(models.Membership.id).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == invite.chapter_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise conflict("already_member")
    membership = models.Membership(
        user_id=user.id,
        chapter_id=invite.chapter_id,
        role=invite.role,
    )
    session.add(membership)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent double-tap/retry race on the (user_id, chapter_id) unique
        # constraint — surface the same graceful 409 (SECURITY-REVIEW finding 6).
        await session.rollback()
        raise conflict("already_member") from None
    await session.refresh(membership)
    return MembershipOut.model_validate(membership)


@router.get("/me/memberships")
async def list_my_memberships(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MembershipOut]:
    """List the caller's ACTIVE memberships, each with its chapter's name joined in.

    Uses get_current_user, not get_current_membership — get_current_membership needs
    a chapter_id already in the path, which is exactly the chicken-and-egg this route
    solves: it's how the client first learns its own chapter_id/role.
    """
    result = await session.execute(
        select(models.Membership, models.Chapter.org_name, models.Chapter.chapter_name)
        .join(models.Chapter, models.Chapter.id == models.Membership.chapter_id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
        .order_by(models.Membership.joined_at)
    )
    memberships: list[MembershipOut] = []
    for membership, org_name, chapter_name in result.all():
        out = MembershipOut.model_validate(membership)
        out.org_name = org_name
        out.chapter_name = chapter_name
        memberships.append(out)
    return memberships
