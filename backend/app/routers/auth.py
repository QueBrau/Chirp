"""Auth router: POST /auth/bootstrap creates the users row; GET /auth/me returns it."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.errors import conflict, not_found
from app.db import get_session
from app.middleware.auth import get_user_by_uid, get_verified_identity
from app.schemas.identity import MeOut, MembershipOut, UserCreate, UserOut

router = APIRouter(tags=["auth"])


@router.post(
    "/auth/bootstrap",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_account(
    body: UserCreate,
    identity: tuple[str, str | None] = Depends(get_verified_identity),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Register the authenticated-but-unregistered identity; 409 if the uid already has a row.

    SECURITY-REVIEW finding 3: in firebase mode, the verified token's email claim is
    authoritative — a body.email that disagrees is rejected (400) rather than trusted,
    closing the email-squatting path where an attacker bootstraps first with a victim's
    email and permanently blocks the victim's own signup. Emulated mode has no verified
    token to check against, so body.email is still trusted there (dev/test only).
    """
    uid, verified_email = identity
    if get_settings().auth_mode == "firebase" and verified_email is not None:
        if body.email != verified_email:
            raise HTTPException(status_code=400, detail="email_mismatch")

    existing = await session.execute(
        select(models.User.id).where(models.User.firebase_uid == uid)
    )
    if existing.scalar_one_or_none() is not None:
        raise conflict("already_registered")

    user = models.User(
        firebase_uid=uid,
        email=body.email,
        display_name=body.display_name,
        avatar_url=body.avatar_url,
        account_type=body.account_type,
        campus_id=body.campus_id,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("email_already_registered") from None
    await session.refresh(user)
    await session.commit()
    return UserOut.model_validate(user)


@router.get("/auth/me", response_model=MeOut)
async def get_me(
    identity: tuple[str, str | None] = Depends(get_verified_identity),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    """Return the caller's user row and active memberships.

    Resolves the user from the verified identity directly rather than via
    get_current_user, since that dependency 401s on an unregistered uid — here an
    authenticated-but-unregistered caller must see 404 user_not_registered instead.
    """
    uid, _verified_email = identity
    user = await get_user_by_uid(session, uid)
    if user is None:
        raise not_found("user_not_registered")

    memberships = await session.execute(
        select(models.Membership)
        .where(models.Membership.user_id == user.id, models.Membership.status == "active")
        .order_by(models.Membership.joined_at)
    )
    return MeOut(
        user=UserOut.model_validate(user),
        memberships=[MembershipOut.model_validate(m) for m in memberships.scalars().all()],
    )
