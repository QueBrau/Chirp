"""Alumni network: own-profile upsert, directory, and job board CRUD."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found
from app.core.permissions import EBOARD, Role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.alumni import (
    AlumniProfileOut,
    AlumniProfileUpdate,
    JobPostCreate,
    JobPostOut,
)

router = APIRouter(tags=["alumni"])


def _caller_chapter_ids(user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Subquery of chapter ids where the user holds an active membership."""
    return select(models.Membership.chapter_id).where(
        models.Membership.user_id == user_id,
        models.Membership.status == "active",
    )


# ---- alumni profile ----


@router.get("/alumni/profile")
async def get_own_profile(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AlumniProfileOut:
    """Return the caller's alumni profile, or 404 if not yet created."""
    profile = await session.get(models.AlumniProfile, user.id)
    if profile is None:
        raise not_found("alumni_profile_not_found")
    out = AlumniProfileOut.model_validate(profile)
    out.display_name = user.display_name
    return out


@router.put("/alumni/profile")
async def upsert_own_profile(
    body: AlumniProfileUpdate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AlumniProfileOut:
    """Create or fully replace the caller's own alumni profile."""
    profile = await session.get(models.AlumniProfile, user.id)
    if profile is None:
        profile = models.AlumniProfile(user_id=user.id)
        session.add(profile)
    profile.grad_year = body.grad_year
    profile.company = body.company
    profile.title = body.title
    profile.industry = body.industry
    profile.linkedin_url = body.linkedin_url
    profile.open_to_mentoring = body.open_to_mentoring
    await session.commit()
    await session.refresh(profile)
    out = AlumniProfileOut.model_validate(profile)
    out.display_name = user.display_name
    return out


@router.get("/alumni/directory")
async def list_directory(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AlumniProfileOut]:
    """Alumni profiles of users sharing a chapter with the caller.

    v1 scope: "shares a chapter" = both hold an active membership (any role,
    including alumni) in at least one common chapter. No campus-wide or
    network-wide discovery yet, and no pagination — chapters are small.
    """
    result = await session.execute(
        select(models.AlumniProfile, models.User)
        .join(models.User, models.User.id == models.AlumniProfile.user_id)
        .join(
            models.Membership,
            models.Membership.user_id == models.AlumniProfile.user_id,
        )
        .where(
            models.Membership.status == "active",
            models.Membership.chapter_id.in_(_caller_chapter_ids(user.id)),
            models.AlumniProfile.user_id != user.id,
        )
        .distinct()
    )
    entries: list[AlumniProfileOut] = []
    for profile, profile_user in result.all():
        out = AlumniProfileOut.model_validate(profile)
        out.display_name = profile_user.display_name
        entries.append(out)
    return entries


# ---- job board ----


@router.post("/jobs", status_code=201)
async def create_job_post(
    body: JobPostCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JobPostOut:
    """Post a job; alumni or e-board members only."""
    allowed_roles = {role.value for role in EBOARD} | {Role.alumni.value}
    role_result = await session.execute(
        select(models.Membership.id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
            models.Membership.role.in_(allowed_roles),
        )
        .limit(1)
    )
    is_eligible = (
        role_result.scalar_one_or_none() is not None or user.account_type == "alumni"
    )
    if not is_eligible:
        raise forbidden("alumni_or_eboard_only")

    if body.chapter_id is not None:
        # Chapter-scoped posts require membership in that chapter (§8.4 spirit).
        member_result = await session.execute(
            select(models.Membership.id)
            .where(
                models.Membership.user_id == user.id,
                models.Membership.chapter_id == body.chapter_id,
                models.Membership.status == "active",
            )
            .limit(1)
        )
        if member_result.scalar_one_or_none() is None:
            raise forbidden("not_a_member")

    job = models.JobPost(
        posted_by=user.id,
        chapter_id=body.chapter_id,
        title=body.title,
        company=body.company,
        description=body.description,
        apply_url=body.apply_url,
        expires_at=body.expires_at,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return JobPostOut.model_validate(job)


@router.get("/jobs")
async def list_job_posts(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[JobPostOut]:
    """List network-wide jobs plus jobs scoped to the caller's chapters; no expired."""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(models.JobPost)
        .where(
            or_(
                models.JobPost.chapter_id.is_(None),
                models.JobPost.chapter_id.in_(_caller_chapter_ids(user.id)),
            ),
            or_(
                models.JobPost.expires_at.is_(None),
                models.JobPost.expires_at > now,
            ),
        )
        .order_by(models.JobPost.created_at.desc())
    )
    return [JobPostOut.model_validate(j) for j in result.scalars().all()]


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job_post(
    job_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a job post; poster only."""
    job = await session.get(models.JobPost, job_id)
    if job is None:
        raise not_found("job_not_found")
    if job.posted_by != user.id:
        raise forbidden("not_poster")
    await session.delete(job)
    await session.commit()
