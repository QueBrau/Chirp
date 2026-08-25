"""Role term lifecycle: keeps role_terms in sync with membership.role (board card c83)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


async def open_initial_term(session: AsyncSession, *, membership: models.Membership) -> None:
    """Seed a membership's first open role_terms row from its just-assigned role.

    Not part of c83's explicit build list (which scoped the write side to the
    role-CHANGE path in PATCH /chapters/{chapter_id}/members), but required for
    that path's own "set ended_at=now on the open term" to describe reality for
    memberships created AFTER 0021 lands: the only two places a membership comes
    into existence — create_chapter's founding president and join_chapter's invite
    redemption — otherwise leave a membership with NO role_terms row at all until
    its first PATCH, which both breaks the one-open-term invariant's intent
    (nothing describes the role the member ALREADY holds) and, worse, means that
    first PATCH's "close the old term" finds nothing to close, silently losing the
    member's founding role from history.

    Mirrors 0021's own backfill exactly: changed_by is NULL because there is no
    ADMIN changing anyone's role here, only a membership beginning to exist — same
    reasoning the migration's docstring gives for its backfilled rows.

    Requires membership.id to already be populated (caller must have flushed the
    just-added Membership row first).
    """
    session.add(
        models.RoleTerm(
            membership_id=membership.id,
            role=membership.role,
            started_at=datetime.now(timezone.utc),
            changed_by=None,
        )
    )


async def apply_role_change(
    session: AsyncSession,
    *,
    membership: models.Membership,
    new_role: str,
    changed_by: uuid.UUID,
) -> None:
    """Close the membership's open role_terms row and open a new one for new_role,
    then sync membership.role — the write side of Jose's c83 ruling that a chapter
    role is a DATED TERM, not a plain fact.

    NO-OP when new_role == membership.role (board card c83, explicit): only an
    ACTUAL change should cut a new term. Without this, re-saving a member's existing
    role from a form that always sends every field would fabricate a term boundary
    (and a changed_by "change") for a role that never changed.

    Does not commit. The caller (routers/chapters.py update_member) may also be
    changing status/pledge_class in the same request and commits once at the end,
    so the term flip and the membership.role write land in one transaction — never
    a role update that "took" on the column but not the history, or vice versa.

    TOLERANT of a membership with no open term to close (nothing found — a plain
    no-op there, straight to inserting the new one). Every membership present when
    0021 ran has one from its backfill, but a membership created afterward via
    create_chapter/join_chapter is NOT (also) seeded one by this card's explicit
    build scope — see the c83 build notes. Heals into the one-open-term invariant
    on its first real role change rather than raising on a state this service
    itself did not create.
    """
    if new_role == membership.role:
        return
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(models.RoleTerm).where(
            models.RoleTerm.membership_id == membership.id,
            models.RoleTerm.ended_at.is_(None),
        )
    )
    open_term = result.scalar_one_or_none()
    if open_term is not None:
        open_term.ended_at = now
    session.add(
        models.RoleTerm(
            membership_id=membership.id,
            role=new_role,
            started_at=now,
            changed_by=changed_by,
        )
    )
    membership.role = new_role
