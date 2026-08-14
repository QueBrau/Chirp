"""Chapter events (Partiful-style, DESIGN §8.7) and RSVPs.

Any active member may host an event — creation is not e-board-gated, unlike
meetings.py. Reads/writes are otherwise scoped through chapter membership the
same way feed.py scopes posts.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.events import (
    EventCreate,
    EventOut,
    EventRsvpOut,
    EventRsvpUpdate,
    EventWithRsvpsOut,
)

router = APIRouter(tags=["events"])


async def _event_with_membership(
    event_id: uuid.UUID,
    user: models.User,
    session: AsyncSession,
) -> tuple[models.Event, models.Membership]:
    """Load an event and the caller's active membership in the event's chapter.

    /events/{event_id}/rsvps has no chapter_id path param, so org scoping is
    checked through the event's chapter (§8.4 spirit): 404 if the event is
    missing, 403 if the caller is not an active member.
    """
    event = await session.get(models.Event, event_id)
    if event is None:
        raise not_found("event_not_found")
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == event.chapter_id,
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise forbidden("not_a_member")
    return event, membership


@router.get("/chapters/{chapter_id}/events")
async def list_events(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    """List the chapter's events, newest first."""
    result = await session.execute(
        select(models.Event)
        .where(models.Event.chapter_id == chapter_id)
        .order_by(models.Event.created_at.desc())
    )
    return [EventOut.model_validate(e) for e in result.scalars().all()]


@router.get("/chapters/{chapter_id}/events-with-rsvps")
async def list_events_with_rsvps(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[EventWithRsvpsOut]:
    """The chapter's events, newest first, each with all its RSVPs (c43).

    Collapses the Events segment's 1+N (listEvents + listRsvps per event) into
    two queries total: one for the events, one IN-clause for their RSVPs.
    """
    result = await session.execute(
        select(models.Event)
        .where(models.Event.chapter_id == chapter_id)
        .order_by(models.Event.created_at.desc())
    )
    events = result.scalars().all()
    rsvps_by_event: dict[uuid.UUID, list[EventRsvpOut]] = {}
    if events:
        rsvp_rows = await session.execute(
            select(models.EventRsvp)
            .where(models.EventRsvp.event_id.in_([event.id for event in events]))
            .order_by(models.EventRsvp.created_at)
        )
        for rsvp in rsvp_rows.scalars().all():
            rsvps_by_event.setdefault(rsvp.event_id, []).append(EventRsvpOut.model_validate(rsvp))
    return [
        EventWithRsvpsOut(
            event=EventOut.model_validate(event),
            rsvps=rsvps_by_event.get(event.id, []),
        )
        for event in events
    ]


@router.post("/chapters/{chapter_id}/events", status_code=201)
async def create_event(
    chapter_id: uuid.UUID,
    body: EventCreate,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Create an event hosted by the caller; any active member may host."""
    event = models.Event(
        chapter_id=chapter_id,
        host_id=membership.user_id,
        title=body.title,
        cover_url=body.cover_url,
        date_label=body.date_label,
        location=body.location,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return EventOut.model_validate(event)


@router.get("/events/{event_id}/rsvps")
async def list_rsvps(
    event_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventRsvpOut]:
    """List an event's RSVPs; scoped through the event's chapter."""
    await _event_with_membership(event_id, user, session)
    result = await session.execute(
        select(models.EventRsvp)
        .where(models.EventRsvp.event_id == event_id)
        .order_by(models.EventRsvp.created_at)
    )
    return [EventRsvpOut.model_validate(r) for r in result.scalars().all()]


@router.put("/events/{event_id}/rsvps")
async def upsert_rsvp(
    event_id: uuid.UUID,
    body: EventRsvpUpdate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventRsvpOut:
    """Upsert the caller's RSVP (going/maybe/cant); scoped through the event's chapter."""
    await _event_with_membership(event_id, user, session)
    rsvp = await session.get(models.EventRsvp, (event_id, user.id))
    if rsvp is None:
        rsvp = models.EventRsvp(event_id=event_id, user_id=user.id, status=body.status)
        session.add(rsvp)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent double-tap raced us to insert the same (event_id, user_id)
            # RSVP row — rollback and apply the status to the row that won instead.
            await session.rollback()
            rsvp = await session.get(models.EventRsvp, (event_id, user.id))
            if rsvp is None:
                raise
            rsvp.status = body.status
            await session.commit()
        else:
            await session.refresh(rsvp)
    else:
        rsvp.status = body.status
        await session.commit()
    return EventRsvpOut.model_validate(rsvp)
