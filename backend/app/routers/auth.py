"""Auth router: POST /auth/bootstrap creates the users row; GET /auth/me returns it.

Also serves GET /campuses/{campus_id} — campus identity, so screens can render the
caller's real campus name instead of a hardcoded one (c46).
"""
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.analytics import emit
from app.core.errors import conflict, not_found
from app.db import get_session
from app.middleware.auth import get_current_user, get_user_by_uid, get_verified_identity
from app.schemas.identity import (
    CampusOut,
    MeOut,
    MembershipOut,
    ProfileUpdate,
    UserCreate,
    UserOut,
)
from app.services.storage_service import (
    AVATAR_PREFIX,
    finalize_media_object,
    validate_media_object_names,
)

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
        # campus_id is deliberately NOT set from the body (c85). A new account has no
        # campus until it proves one; that is the correct state for someone who has
        # verified nothing, not an edge case. The .edu redemption in c86 is the only
        # writer of this column.
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("email_already_registered") from None
    await session.refresh(user)
    await session.commit()
    emit("user_signed_up", user_id=user.id, account_type=user.account_type)
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


@router.patch("/auth/me", response_model=UserOut)
async def update_me(
    body: ProfileUpdate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Edit your own profile: display name and profile picture (board c221).

    SELF ONLY, by construction. There is no user id in the path and none in the body -
    the row edited is whatever get_current_user resolved, so there is no shape in which
    this route can be pointed at somebody else's profile.

    THE AVATAR IS FINALIZED TO avatars/, NOT posts/, AND THAT IS LOAD-BEARING.
    jobs/media_reconcile.py builds its reference set from `select(Post.media_urls)` and
    nothing else, then deletes anything under posts/ that is not in it. users.avatar_url
    is not in that set, so an avatar finalized to posts/ would be unreferenced by
    definition and collected about a day after the user set it. Leaving it in tmp/ is no
    better - the age-based lifecycle rule scoped to tmp/ exists precisely to reap
    abandoned uploads (c132). See c221.

    OMITTED VS EXPLICIT NULL: model_fields_set distinguishes "leave it alone" from
    "clear it", so sending {"avatar_object_name": null} removes the picture and falls
    back to initials, while omitting the field entirely keeps whatever is stored.

    to_thread around finalize_media_object for the reason c211 established: it is a
    synchronous function that makes GCS network calls, and this process runs one uvicorn
    worker at concurrency 80, so calling it inline stalls every other in-flight request
    for the duration of a copy.
    """
    fields = body.model_fields_set

    if "display_name" in fields:
        if body.display_name is None:
            # display_name is NOT NULL on the row and is the only name anything renders,
            # so clearing it is refused rather than quietly ignored.
            raise HTTPException(status_code=422, detail="display_name_cannot_be_cleared")
        user.display_name = body.display_name

    if "avatar_object_name" in fields:
        if body.avatar_object_name is None:
            # Removing the picture. The old object is deliberately NOT deleted here:
            # chirp-api-run has no delete grant outside tmp/ by design (c132), and
            # widening it is the exact thing that card exists to prevent.
            user.avatar_url = None
        else:
            # Same gate post create uses: the name must be THIS caller's own tmp/
            # upload. Object names are opaque UUIDs but not secret, and the bucket is
            # public-read, so without this one caller could claim another's upload.
            validate_media_object_names(str(user.id), [body.avatar_object_name])
            user.avatar_url = await asyncio.to_thread(
                finalize_media_object,
                str(user.id),
                body.avatar_object_name,
                destination_prefix=AVATAR_PREFIX,
            )

    await session.commit()
    await session.refresh(user)
    return UserOut.model_validate(user)


@router.get("/campuses/{campus_id}", response_model=CampusOut)
async def get_campus(
    campus_id: uuid.UUID,
    _user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CampusOut:
    """Return a campus by id; any registered caller may read one (c46).

    Campus name/slug are public-facing labels shown on Profile and the Chirp board
    header. Before this route the app had no way to resolve users.campus_id to a
    name, so those screens hardcoded a mock campus — the bug this fixes.
    """
    campus = await session.get(models.Campus, campus_id)
    if campus is None:
        raise not_found("campus_not_found")
    return CampusOut.model_validate(campus)
