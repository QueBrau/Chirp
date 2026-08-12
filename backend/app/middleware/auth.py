"""Authentication dependencies: verified Firebase uid and registered-user resolution."""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session


async def get_verified_uid(
    x_debug_firebase_uid: str | None = Header(default=None, alias="X-Debug-Firebase-Uid"),
    authorization: str | None = Header(default=None),
) -> str:
    """Return the caller's verified Firebase uid, or raise 401.

    Emulated mode trusts the X-Debug-Firebase-Uid header; firebase mode verifies the
    Authorization: Bearer <id-token> via firebase_admin (imported lazily so the
    dependency stays optional for local dev).
    """
    settings = get_settings()
    if settings.auth_mode == "emulated":
        if not x_debug_firebase_uid:
            raise HTTPException(status_code=401, detail="missing_debug_uid")
        return x_debug_firebase_uid

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth

        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app()
        decoded = firebase_auth.verify_id_token(token)
    except Exception:  # invalid/expired token, missing SDK, or init failure
        raise HTTPException(status_code=401, detail="invalid_token")
    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="invalid_token")
    return uid


async def get_current_user(
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """Resolve the verified uid to a registered users row, or raise 401.

    Only POST /auth/bootstrap uses get_verified_uid directly (it creates the row).
    """
    result = await session.execute(select(models.User).where(models.User.firebase_uid == uid))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="user_not_registered")
    return user
