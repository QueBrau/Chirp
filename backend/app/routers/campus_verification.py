""".edu campus verification endpoints: request a code, redeem a code (c86).

Kept out of routers/auth.py deliberately. Bootstrap and session resolution are the
paths every request depends on, and this is a self-contained flow with its own failure
modes; a mistake here should not be one file away from the login path.

The response shape is built for c90's verify screen, which needs to tell its states
apart — sent, wrong, expired, rate-limited, verified — so each failure returns its own
machine-readable detail rather than a generic 403. A screen that cannot distinguish
"wrong code" from "expired" shows the user the wrong instruction.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.analytics import emit
from app.core.campus_access import is_campus_verified
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.campus_verification import (
    CampusVerificationRedeem,
    CampusVerificationStart,
    CampusVerificationStatus,
    PendingVerificationOut,
)
from app.services import campus_verification

router = APIRouter(tags=["campus-verification"])


@router.get("/auth/campus-verification")
async def verification_status(
    user: models.User = Depends(get_current_user),
) -> CampusVerificationStatus:
    """Whether the caller currently holds a valid .edu verification.

    c90 opens on this: it decides between "verify your .edu", "your verification
    expired" and not showing the screen at all. `verified_at` is returned even when
    `verified` is false so the screen can tell a LAPSED verification from one that
    never happened — different copy, and a returning student deserves the right one.
    """
    return CampusVerificationStatus(
        verified=is_campus_verified(user),
        verified_at=user.campus_verified_at,
        campus_id=user.campus_id,
    )


@router.post("/auth/campus-verification", status_code=202)
async def start_verification(
    body: CampusVerificationStart,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PendingVerificationOut:
    """Mail a one-time code to an .edu address whose domain maps to a known campus.

    202, not 201: the code has been accepted for delivery, and whether it lands is the
    mail provider's business. The campus is resolved from the address server-side and
    is never taken from the body — see the service module for why that matters.
    """
    verification = await campus_verification.start_verification(session, user, body.edu_email)
    emit(
        "campus_verification_started",
        user_id=user.id,
        campus_id=verification.campus_id,
    )
    return PendingVerificationOut(
        edu_email=verification.edu_email,
        campus_id=verification.campus_id,
        expires_at=verification.expires_at,
    )


@router.post("/auth/campus-verification/redeem")
async def redeem_verification(
    body: CampusVerificationRedeem,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CampusVerificationStatus:
    """Redeem a code and record the proof; opens the campus feed and Chirp."""
    await campus_verification.redeem(session, user, body.code)
    emit("campus_verification_redeemed", user_id=user.id, campus_id=user.campus_id)
    return CampusVerificationStatus(
        verified=is_campus_verified(user),
        verified_at=user.campus_verified_at,
        campus_id=user.campus_id,
    )
