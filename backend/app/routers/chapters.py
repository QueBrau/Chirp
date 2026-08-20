"""Chapter CRUD, member management, invite creation, and invite-code join."""
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import conflict, forbidden, not_found
from app.core.invites import clamp_invite_expiry
from app.core.permissions import EBOARD, MEMBERS_ADMIN, Role, capabilities_for, require_role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.identity import (
    ChapterCreate,
    ChapterInviteCreate,
    ChapterInviteOut,
    ChapterInviteRevokeRequest,
    ChapterJoinRequest,
    ChapterOut,
    MemberOut,
    MembershipOut,
    MembershipUpdate,
    RoleMetaOut,
)

router = APIRouter(tags=["chapters"])

_EBOARD_ROLE_VALUES: frozenset[str] = frozenset(role.value for role in EBOARD)


@router.post("/chapters", status_code=201)
async def create_chapter(
    body: ChapterCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChapterOut:
    """Create a chapter; the creator becomes its president via a new membership.

    Platform-admin only (SECURITY-REVIEW finding 1 / board card c28): self-serve
    chapter creation was the last privilege-escalation vector, since the
    creator auto-becomes president (full EBOARD powers). There is no API to
    grant is_platform_admin — it is flipped directly in the DB.
    """
    if not user.is_platform_admin:
        raise forbidden("platform_admin_required")
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
    # c96, same rule as join_chapter: the founding president belongs to the
    # campus they just created a chapter on. Safe here for the stronger reason
    # that this route is platform-admin-only.
    if user.campus_id is None:
        user.campus_id = chapter.campus_id
        session.add(user)
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
) -> list[MemberOut]:
    """List the chapter's memberships with display identity; org-scoped (§8.4)."""
    result = await session.execute(
        select(models.Membership, models.User)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(models.Membership.chapter_id == chapter_id)
        .order_by(models.Membership.joined_at)
    )
    entries: list[MemberOut] = []
    for membership, member_user in result.all():
        entries.append(
            MemberOut(
                id=membership.id,
                user_id=membership.user_id,
                chapter_id=membership.chapter_id,
                role=membership.role,
                status=membership.status,
                pledge_class=membership.pledge_class,
                joined_at=membership.joined_at,
                display_name=member_user.display_name,
                avatar_url=member_user.avatar_url,
            )
        )
    return entries


@router.get("/chapters/{chapter_id}/role-meta")
async def get_role_meta(
    chapter_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
) -> RoleMetaOut:
    """Role taxonomy for this chapter's UI; org-scoped (§8.4).

    Derived entirely from permissions.py so the app never hand-mirrors the
    eboard set or the invite rule (c44). `invitable` applies the create_invite
    rule for THIS caller: any e-board role may mint non-eboard invites, only a
    president may mint e-board invites, everyone else gets an empty list.
    """
    roles = [role.value for role in Role]
    eboard = [role.value for role in Role if role in EBOARD]
    non_eboard = [role.value for role in Role if role not in EBOARD]
    if membership.role == Role.president.value:
        invitable = non_eboard + eboard
    elif membership.role in _EBOARD_ROLE_VALUES:
        invitable = non_eboard
    else:
        invitable = []
    return RoleMetaOut(
        roles=roles,
        eboard=eboard,
        invitable=invitable,
        capabilities=capabilities_for(membership.role),
    )


@router.patch("/chapters/{chapter_id}/members")
async def update_member(
    chapter_id: uuid.UUID,
    body: MembershipUpdate,
    _actor: models.Membership = Depends(require_role(*MEMBERS_ADMIN)),
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
    """Create a deep-link invite code; e-board only. Bounded expiry, bounded uses.

    SECURITY-REVIEW finding 2: minting an EBOARD-role invite (e.g. a historian
    inviting a future president) requires the creator to already be president —
    any e-board role may still mint non-eboard invites (member/pledge/alumni).

    c105: this endpoint used to hand back a bearer token. `expires_at` was optional
    and passed straight through, so the documented, default way to call it produced
    a code that worked forever for anyone who ever saw the string. Every code now
    gets an expiry whether or not one was asked for, and a redemption budget.
    """
    if body.role in _EBOARD_ROLE_VALUES and actor.role != Role.president.value:
        raise forbidden("insufficient_role")
    now = datetime.now(timezone.utc)
    if body.expires_at is not None:
        requested = body.expires_at
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        if requested <= now:
            raise HTTPException(status_code=422, detail="invite_expiry_in_past")
    invite = models.ChapterInvite(
        chapter_id=chapter_id,
        code=secrets.token_urlsafe(9),
        role=body.role,
        expires_at=clamp_invite_expiry(body.expires_at, now),
        max_uses=body.max_uses,
        created_by=actor.user_id,
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return ChapterInviteOut.model_validate(invite)


@router.get("/chapters/{chapter_id}/invites")
async def list_invites(
    chapter_id: uuid.UUID,
    actor: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> list[ChapterInviteOut]:
    """Every invite this chapter has minted; e-board only.

    c111: c105 shipped revocation that took the CODE, which covers a president who
    still has the string in front of them and nobody else. Minting was the only
    place a code was ever returned, so a code posted three weeks ago and forwarded
    twice could not be revoked at all — the route existed and could not be reached.

    Returns revoked and expired codes too, deliberately. "Which of my codes are
    still live" is answerable from this list, and hiding the dead ones would make a
    leaked-but-expired code look like it was never minted, which is the question a
    president is actually asking when they come here.

    Ordered by expires_at descending, which is FURTHEST-FROM-EXPIRY first and not
    the same thing as newest first. `chapter_invites` has no created_at column, so
    minting order is not recoverable from this table at all — a code minted today
    with the 7-day default sorts below one minted last week for 30 days. It is the
    closest proxy available without a migration, and it does put live codes above
    dead ones, which is the question the screen is for. A created_at is worth
    adding the next time this table is touched.
    """
    result = await session.execute(
        select(models.ChapterInvite)
        .where(models.ChapterInvite.chapter_id == chapter_id)
        .order_by(models.ChapterInvite.expires_at.desc())
    )
    return [ChapterInviteOut.model_validate(row) for row in result.scalars().all()]


@router.post("/chapters/{chapter_id}/invites/revoke", status_code=200)
async def revoke_invite(
    chapter_id: uuid.UUID,
    body: ChapterInviteRevokeRequest,
    actor: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> ChapterInviteOut:
    """Kill a leaked invite code. E-board only, and only for their own chapter.

    c105: before this there was NO way to withdraw a code short of an edit against
    the prod database. Revoking is idempotent — a second call on an already-revoked
    code returns the same row rather than 409, because the caller's intent ("this
    code must not work") is already satisfied and an error would only push them to
    go looking for something else to do.

    Scoped by chapter_id in the WHERE clause, not just by the role dependency: the
    dependency proves you are e-board SOMEWHERE, and looking the code up globally
    would let one chapter's president revoke another chapter's invites.
    """
    result = await session.execute(
        select(models.ChapterInvite).where(
            models.ChapterInvite.code == body.code,
            models.ChapterInvite.chapter_id == chapter_id,
        )
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise not_found("invite_not_found")
    if invite.role in _EBOARD_ROLE_VALUES and actor.role != Role.president.value:
        raise forbidden("insufficient_role")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
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
    """Redeem an invite code: validates expiry, revocation and the redemption
    budget, 409 if already a member.

    c105: this used to be the whole check — does the row exist, has it expired —
    which made a code an unlimited-use bearer token. It now also has to be
    un-revoked and have a seat left, and the seat is CLAIMED rather than counted.
    """
    result = await session.execute(
        select(models.ChapterInvite).where(models.ChapterInvite.code == body.code)
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise not_found("invite_not_found")
    now = datetime.now(timezone.utc)
    # These three read the row we just loaded and exist to give the caller a
    # specific reason. They are NOT the enforcement — the conditional UPDATE below
    # is, because between this read and that write another redemption can land.
    if invite.revoked_at is not None:
        raise forbidden("invite_revoked")
    if invite.expires_at <= now:
        raise forbidden("invite_expired")
    if invite.uses >= invite.max_uses:
        raise forbidden("invite_exhausted")
    existing = await session.execute(
        select(models.Membership.id).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == invite.chapter_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise conflict("already_member")
    # c105 — CLAIM a seat, do not count one.
    #
    # Read-check-then-write on `uses` is the dues double-charge bug wearing a
    # different hat: two people redeeming the last seat both read uses=24, both
    # decide there is room, and both write 25. A single conditional UPDATE makes
    # the database do the deciding, and it re-tests expiry and revocation in the
    # same statement so a code revoked one millisecond ago cannot slip through the
    # gap between the SELECT above and this write.
    claimed = await session.execute(
        update(models.ChapterInvite)
        .where(
            models.ChapterInvite.id == invite.id,
            models.ChapterInvite.uses < models.ChapterInvite.max_uses,
            models.ChapterInvite.revoked_at.is_(None),
            models.ChapterInvite.expires_at > now,
        )
        .values(uses=models.ChapterInvite.uses + 1)
        .returning(models.ChapterInvite.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        raise forbidden("invite_exhausted")

    membership = models.Membership(
        user_id=user.id,
        chapter_id=invite.chapter_id,
        role=invite.role,
    )
    session.add(membership)

    # c96 — a chapter you were INVITED to is proof of a campus, so inherit it.
    #
    # Before this, nothing anywhere wrote users.campus_id: c85 correctly stopped
    # trusting the client to assert one at bootstrap, and named c86's .edu
    # redemption as the only writer. But c86 is deferred, so every user sat at
    # campus_id NULL forever, which dead-ends Home's Campus tab AND the whole Yak
    # tab and makes board gate c71 unreachable.
    #
    # This is NOT a rollback of c85. The value is read off the CHAPTER, which
    # only a platform admin can create and which carries a server-set campus_id;
    # the client supplies an invite code and nothing else. An e-board member
    # deliberately minting a code for you is a human vouching for you, which is
    # the same kind of evidence .edu verification gathers, arriving earlier.
    #
    # Only fills a NULL. Once c86 ships, a verified .edu is the stronger claim
    # and must win, so this must never overwrite a campus the user already has.
    if user.campus_id is None:
        chapter = await session.get(models.Chapter, invite.chapter_id)
        if chapter is not None:
            user.campus_id = chapter.campus_id
            session.add(user)

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
