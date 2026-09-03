"""Alumni network: own-profile upsert, directory, and job board CRUD."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, and_, or_, select, tuple_
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


def _chapter_peer_ids(user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Subquery of user ids holding an active membership in one of the caller's chapters.

    The same "shares a chapter with the caller" relation GET /alumni/directory is
    built on, factored out because GET /jobs now needs it too (c242). Includes the
    caller themselves, which is correct: their own posts must stay visible to them.
    """
    return select(models.Membership.user_id).where(
        models.Membership.status == "active",
        models.Membership.chapter_id.in_(_caller_chapter_ids(user_id)),
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
    out.email = user.email
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
    profile.location = body.location
    profile.linkedin_url = body.linkedin_url
    profile.open_to_mentoring = body.open_to_mentoring
    await session.commit()
    await session.refresh(profile)
    out = AlumniProfileOut.model_validate(profile)
    out.display_name = user.display_name
    out.email = user.email
    return out


@router.get("/alumni/directory")
async def list_directory(
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AlumniProfileOut]:
    """Alumni profiles of users sharing a chapter with the caller.

    v1 scope: "shares a chapter" = both hold an active membership (any role,
    including alumni) in at least one common chapter. No campus-wide or
    network-wide discovery yet, and no pagination — chapters are small.
    """
    directory = (
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
    # ORDER BY ADDED WITH THE CURSOR, and it is not incidental: this query had no
    # ordering at all, so row order was whatever the planner returned. A cursor over an
    # unordered query is meaningless - "everything before X" needs an X that is always
    # in the same place - and an unordered LIMIT truncates arbitrary rows, which is
    # worse than no cap because it is nondeterministic (c258).
    #
    # SINGLE-COLUMN CURSOR ON PURPOSE, unlike every other list in this card.
    # alumni_profiles has NO created_at - the row is a profile, not an event, so there
    # is nothing time-shaped to sort by. user_id is the primary key, so it is unique
    # and total on its own: the (created_at, id) tie-break that every other cursor here
    # needs exists precisely BECAUSE timestamps collide, and a unique sort key cannot.
    # Adding a second column would be ceremony, not safety.
    if before_id is not None:
        directory = directory.where(models.AlumniProfile.user_id < before_id)
    result = await session.execute(
        directory.order_by(models.AlumniProfile.user_id.desc()).limit(limit)
    )
    entries: list[AlumniProfileOut] = []
    for profile, profile_user in result.all():
        out = AlumniProfileOut.model_validate(profile)
        out.display_name = profile_user.display_name
        out.email = profile_user.email
        entries.append(out)
    return entries


# ---- job board ----


@router.post("/jobs", status_code=201)
async def create_job_post(
    body: JobPostCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JobPostOut:
    """Post a job; requires a real e-board or alumni MEMBERSHIP row (c242).

    ELIGIBILITY COMES FROM A MEMBERSHIP, NEVER FROM users.account_type. This check
    used to read `... or user.account_type == "alumni"`, and account_type is a
    self-declared field: the signup body carries it (schemas/identity.py) and
    bootstrap writes it straight to the row. So anyone could tick "alumni" on the
    account-type screen and post to the job board — and every job carries an
    apply_url that the app opens with Linking.openURL, which made this a phishing
    channel gated by a field the attacker sets for themselves. A membership row is
    the opposite kind of fact: the only ways to get one are redeeming an invite code
    minted by an e-board member (routers/chapters.py create_invite) or a president
    changing your role. Nothing a caller can assert about themselves.

    THE ROLE MUST BE HELD IN THE TARGET CHAPTER, not merely somewhere. The old code
    asked two separate questions — "eligible role in ANY chapter?" and "member of
    THIS chapter?" — which let an alumnus of chapter A post to chapter B's board on
    the strength of a plain `member` row there. One query, scoped to the target
    chapter, closes that.

    A network-wide post (chapter_id NULL) has no target chapter to scope to, so it
    requires the qualifying role in at least one chapter. That is the weakest rule
    in here on purpose: see list_job_posts for how far such a post actually reaches.
    """
    eligible_roles = {role.value for role in EBOARD} | {Role.alumni.value}
    memberships = select(models.Membership.role).where(
        models.Membership.user_id == user.id,
        models.Membership.status == "active",
    )
    if body.chapter_id is not None:
        memberships = memberships.where(models.Membership.chapter_id == body.chapter_id)
    roles_held = set((await session.execute(memberships)).scalars().all())

    if body.chapter_id is not None and not roles_held:
        # Not in that chapter at all — the §8.4 answer, kept as its own detail so a
        # non-member and a member of the wrong role stay distinguishable.
        raise forbidden("not_a_member")
    if not roles_held & eligible_roles:
        raise forbidden("alumni_or_eboard_only")

    job = models.JobPost(
        posted_by=user.id,
        chapter_id=body.chapter_id,
        title=body.title,
        company=body.company,
        location=body.location,
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
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[JobPostOut]:
    """Jobs the caller shares a chapter with; no expired.

    SCOPE (c242). This used to return every `chapter_id IS NULL` row to EVERY
    authenticated caller — no campus scope, no chapter scope, network-wide by
    literal default — while each row carries an apply_url the app opens with
    Linking.openURL. One post reached the entire user base. Now:

      chapter-scoped job  -> visible in the chapters the caller belongs to
      network-wide job    -> visible to users who share a chapter with the POSTER

    So a caller with no active membership sees nothing at all, which is the right
    answer: the job board is a chapter benefit, and everything about the surface
    says so — the screen lives under the chapter tab, its subtitle is "Directory
    and chapter job board", and its empty state reads "Alumni and e-board can post
    openings for the chapter".

    `chapter_id IS NULL` STILL MEANS "not tied to one chapter" ON THE ROW — the
    column's meaning is unchanged and no migration is involved. What changed is the
    AUDIENCE such a row gets, which was never written down anywhere except in this
    query. Deriving it from the poster's chapters is the rule GET /alumni/directory
    already uses for people ("shares a chapter with the caller", and its docstring
    is explicit that v1 has no campus-wide or network-wide discovery), applied to
    jobs so the two reads on this router cannot disagree. In practice a NULL post
    now means "all of my chapters at once", which is the one thing a single
    chapter_id cannot express.

    Joins users so each row carries the poster's display name. Without it the
    client gets a bare UUID and there is no GET /users/{id} to resolve it, which
    is exactly why the Jobs screen used to render every poster as "Alumni".
    Inner join, not outer: JobPost.posted_by is a non-null FK to users, so a job
    with no matching user cannot exist and an outer join would only add a null
    branch that can never be taken.
    """
    now = datetime.now(timezone.utc)
    peers = _chapter_peer_ids(user.id)
    jobs = (
        select(models.JobPost, models.User.display_name)
        .join(models.User, models.User.id == models.JobPost.posted_by)
        .where(
            or_(
                and_(
                    models.JobPost.chapter_id.is_(None),
                    models.JobPost.posted_by.in_(peers),
                ),
                models.JobPost.chapter_id.in_(_caller_chapter_ids(user.id)),
            ),
            or_(
                models.JobPost.expires_at.is_(None),
                models.JobPost.expires_at > now,
            ),
        )
    )
    # Cursored on (created_at, id) like every other time-ordered list in c258. Postings
    # accumulate: an expired one drops out of the window, but the board still grows with
    # every season of hiring, so a cap alone would only move the truncation later.
    if before is not None and before_id is not None:
        jobs = jobs.where(
            tuple_(models.JobPost.created_at, models.JobPost.id) < (before, before_id)
        )
    elif before is not None:
        jobs = jobs.where(models.JobPost.created_at < before)
    result = await session.execute(
        jobs.order_by(
            models.JobPost.created_at.desc(), models.JobPost.id.desc()
        ).limit(limit)
    )
    entries: list[JobPostOut] = []
    for job, display_name in result.all():
        out = JobPostOut.model_validate(job)
        out.posted_by_name = display_name
        entries.append(out)
    return entries


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
