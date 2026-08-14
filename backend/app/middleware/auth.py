"""Authentication dependencies: verified Firebase uid and registered-user resolution."""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session


async def _verify_identity(
    x_debug_firebase_uid: str | None,
    authorization: str | None,
) -> tuple[str, str | None]:
    """Shared verification logic: returns (uid, verified_email).

    verified_email is the Firebase token's 'email' claim in firebase mode (None if the
    token carries no such claim), or always None in emulated mode (there is no token to
    verify an email against, so the caller-supplied email must be trusted as-is there).
    """
    settings = get_settings()
    if settings.auth_mode == "emulated":
        if not x_debug_firebase_uid:
            raise HTTPException(status_code=401, detail="missing_debug_uid")
        return x_debug_firebase_uid, None

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth
    except Exception:  # missing SDK
        raise HTTPException(status_code=401, detail="invalid_token")

    # Lazy-init fallback for safety (e.g. test import order) — the app.main lifespan
    # now does this at boot in firebase mode, so a misconfigured deploy fails
    # visibly at startup instead of surfacing here. Left unwrapped below on purpose:
    # a real init failure should propagate, not get masked as an invalid_token 401.
    try:
        firebase_admin.get_app()
    except ValueError:
        # Pin the token audience to the configured Firebase project; without
        # this the SDK infers the ambient GCP project, which rejects tokens
        # whenever the Firebase project differs from where the code runs.
        options = (
            {"projectId": settings.firebase_project_id}
            if settings.firebase_project_id
            else None
        )
        firebase_admin.initialize_app(options=options)

    try:
        decoded = firebase_auth.verify_id_token(token)
    except Exception:  # invalid/expired token
        raise HTTPException(status_code=401, detail="invalid_token")
    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="invalid_token")
    return uid, decoded.get("email")


async def get_verified_uid(
    x_debug_firebase_uid: str | None = Header(default=None, alias="X-Debug-Firebase-Uid"),
    authorization: str | None = Header(default=None),
) -> str:
    """Return the caller's verified Firebase uid, or raise 401.

    Emulated mode trusts the X-Debug-Firebase-Uid header; firebase mode verifies the
    Authorization: Bearer <id-token> via firebase_admin (imported lazily so the
    dependency stays optional for local dev). See get_verified_identity for uid+email.
    """
    uid, _email = await _verify_identity(x_debug_firebase_uid, authorization)
    return uid


async def get_verified_identity(
    x_debug_firebase_uid: str | None = Header(default=None, alias="X-Debug-Firebase-Uid"),
    authorization: str | None = Header(default=None),
) -> tuple[str, str | None]:
    """Return (uid, verified_email) — used by POST /auth/bootstrap to stop email squatting
    (SECURITY-REVIEW finding 3): the verified token email, not the client body, is
    authoritative in firebase mode.
    """
    return await _verify_identity(x_debug_firebase_uid, authorization)


async def get_user_by_uid(session: AsyncSession, uid: str) -> models.User | None:
    """Single home for the uid -> users-row lookup; callers pick their own miss
    semantics (401 here, 404 on /auth/me, 4401 close on the WS gateway)."""
    result = await session.execute(select(models.User).where(models.User.firebase_uid == uid))
    return result.scalar_one_or_none()


async def get_current_user(
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """Resolve the verified uid to a registered users row, or raise 401.

    Only POST /auth/bootstrap uses get_verified_uid directly (it creates the row).
    """
    user = await get_user_by_uid(session, uid)
    if user is None:
        raise HTTPException(status_code=401, detail="user_not_registered")
    return user
