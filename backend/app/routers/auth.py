"""Auth router: POST /auth/bootstrap creates the users row for a verified Firebase uid."""
from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import conflict
from app.db import get_session
from app.middleware.auth import get_verified_uid
from app.schemas.identity import UserCreate, UserOut

router = APIRouter(tags=["auth"])


@router.post(
    "/auth/bootstrap",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_account(
    body: UserCreate,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Register the authenticated-but-unregistered identity; 409 if the uid already has a row."""
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
