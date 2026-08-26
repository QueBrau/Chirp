"""Chapter events (Partiful-style, DESIGN §8.7), invites and RSVPs (c33, c198).

Any active member may host an event - creation is not e-board-gated, unlike meetings.py.
EDITING AND CANCELLING ARE NARROWER than hosting: the host, or the chapter e-board. A
member who can create their own event has no business rewriting somebody else's.

WHO CAN READ ONE EVENT is the security surface of this module, and it is deliberately
two rules rather than one:

    visibility  - who can find the event WITHOUT being invited
    invite      - an explicit grant that admits one named person regardless of tier

So a 'campus' event can still be shown to a specific alum or a sister chapter by
inviting them, and a 'public' event is readable by anyone at all. Reading it the other
way round - an invite that does NOT admit you - makes the invite button meaningless for
exactly the people you would want to invite, which is the whole feature.

THE PUBLIC TIER IS A DELIBERATE HOLE THROUGH c88, opened by braul on c198 with the risk
written down. Three guards keep it the size it was agreed to be, and none is optional:

  1. 'campus' is the default everywhere - schema default, column default. Widening is
     always an explicit act by a host.
  2. The unauthenticated route serialises PublicEventOut, never EventOut. The internet
     learns that a party is happening; it does not learn who is going.
  3. The guest list is never public, at any tier. It needs membership, an invite, or an
     RSVP of your own.

ANONYMOUS RSVP IS NOT IMPLEMENTED and that is a scope call worth stating. 'public' here
means anyone with an ACCOUNT may RSVP, with no .edu verification required - it does not
mean a stranger can add themselves to a guest list without signing in. Storing
name-only guests needs its own abuse story (rate limits, moderation, deletion) and is
its own card.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.campus_access import is_campus_verified
from app.core.errors import forbidden, not_found
from app.core.permissions import EBOARD
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.events import (
    EventCreate,
    EventGuestsOut,
    EventInviteCreate,
    EventInviteOut,
    EventOut,
    EventRsvpOut,
    EventRsvpUpdate,
    EventUpdate,
    EventWithRsvpsOut,
    PublicEventOut,
)

router = APIRouter(tags=["events"])


async def _membership_in(
    chapter_id: uuid.UUID, user: models.User, session: AsyncSession
) -> models.Membership | None:
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def _event_with_membership(
    event_id: uuid.UUID,
    user: models.User,
    session: AsyncSession,
) -> tuple[models.Event, models.Membership]:
    """Load an event and REQUIRE the caller to be an active member of its chapter.

    The strict one, for chapter-internal actions (edit, cancel, invite). Reads use
    `_readable_event` instead, which also admits invitees and the visibility tiers.
    """
    event = await session.get(models.Event, event_id)
    if event is None:
        raise not_found("event_not_found")
    membership = await _membership_in(event.chapter_id, user, session)
    if membership is None:
        raise forbidden("not_a_member")
    return event, membership


async def _readable_event(
    event_id: uuid.UUID,
    user: models.User,
    session: AsyncSession,
) -> models.Event:
    """Load an event the caller is allowed to read, or raise.

    THE ORDER OF THESE CHECKS IS CHEAPEST-AND-BROADEST FIRST, but every branch is a
    real authorisation decision, not a shortcut:

      member of the hosting chapter  - always, it is their own event
      explicitly invited             - always, that is what an invite MEANS
      chapter                        - nobody else; the two rules above are the only ways in
      public                         - any signed-in user
      verified                       - any user holding a current .edu verification
      campus                         - verified AND belonging to this chapter's campus

    404 for a missing event, 403 for one that exists and is not yours to see. The
    difference leaks only that an id exists, which is already true of every other
    /events route in this module.
    """
    event = await session.get(models.Event, event_id)
    if event is None:
        raise not_found("event_not_found")

    if await _membership_in(event.chapter_id, user, session) is not None:
        return event

    invite = await session.get(models.EventInvite, (event_id, user.id))
    if invite is not None:
        return event

    # 'chapter' is the floor and the default, including for every row that predates
    # 0024. Membership and an invite are the only ways past it - deliberately checked
    # BEFORE the verification branches below, so a members-only party never returns
    # "campus_unverified" and hints that verifying would help. It would not.
    if event.visibility == "chapter":
        raise forbidden("not_a_member")

    if event.visibility == "public":
        return event

    if not is_campus_verified(user):
        raise forbidden("campus_unverified")

    if event.visibility == "verified":
        return event

    # 'campus': the narrowest tier. Resolve the chapter's campus off the row rather
    # than trusting anything in the request.
    chapter = await session.get(models.Chapter, event.chapter_id)
    if chapter is None or user.campus_id != chapter.campus_id:
        raise forbidden("not_your_campus")
    return event


def _may_manage(event: models.Event, membership: models.Membership) -> bool:
    """Host, or the chapter's e-board. Hosting is open; rewriting someone else's is not."""
    return membership.user_id == event.host_id or membership.role in EBOARD


@router.get("/chapters/{chapter_id}/events")
async def list_events(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    """List the chapter's events, soonest-first by start time."""
    result = await session.execute(
        select(models.Event)
        .where(models.Event.chapter_id == chapter_id)
        .order_by(models.Event.starts_at.desc())
    )
    return [EventOut.model_validate(e) for e in result.scalars().all()]


@router.get("/chapters/{chapter_id}/events-with-rsvps")
async def list_events_with_rsvps(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[EventWithRsvpsOut]:
    """The chapter's events with all their RSVPs (c43).

    Collapses the Events segment's 1+N (listEvents + listRsvps per event) into two
    queries total: one for the events, one IN-clause for their RSVPs.
    """
    result = await session.execute(
        select(models.Event)
        .where(models.Event.chapter_id == chapter_id)
        .order_by(models.Event.starts_at.desc())
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
        description=body.description,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        location=body.location,
        visibility=body.visibility,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return EventOut.model_validate(event)


@router.get("/events/{event_id}")
async def get_event(
    event_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """One event, if the caller may read it. See `_readable_event`."""
    event = await _readable_event(event_id, user, session)
    return EventOut.model_validate(event)


@router.patch("/events/{event_id}")
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Edit an event. Host or e-board only.

    A CANCELLED EVENT IS NOT EDITABLE. Re-opening one by clearing canceled_at is not
    reachable here on purpose: people were told it was off, and silently reviving the
    same row would leave their RSVPs attached to a party they last heard was cancelled.
    Host a new one.
    """
    event, membership = await _event_with_membership(event_id, user, session)
    if not _may_manage(event, membership):
        raise forbidden("not_the_host")
    if event.canceled_at is not None:
        raise forbidden("event_canceled")

    fields = body.model_dump(exclude_unset=True)
    for name, value in fields.items():
        setattr(event, name, value)

    # Re-check the ordering across the MERGED state, not just the submitted fields:
    # moving starts_at past an existing ends_at is the ordinary way to break this, and
    # the submitted body alone cannot see it. The CHECK constraint would catch it as a
    # 500; this makes it a 422 that names the problem.
    if event.ends_at is not None and event.ends_at <= event.starts_at:
        await session.rollback()
        raise HTTPException(status_code=422, detail="ends_at_must_be_after_starts_at")

    await session.commit()
    await session.refresh(event)
    return EventOut.model_validate(event)


@router.post("/events/{event_id}/cancel")
async def cancel_event(
    event_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Call off an event. Host or e-board only.

    A TIMESTAMP, NOT A DELETE. The row and its guest list survive so the event can
    render as "Canceled" to everyone who RSVPd - the exact people who need telling.
    Idempotent: cancelling twice keeps the first timestamp, because the moment it was
    called off is the fact worth keeping.
    """
    event, membership = await _event_with_membership(event_id, user, session)
    if not _may_manage(event, membership):
        raise forbidden("not_the_host")
    if event.canceled_at is None:
        event.canceled_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(event)
    return EventOut.model_validate(event)


@router.post("/events/{event_id}/invites", status_code=201)
async def invite_to_event(
    event_id: uuid.UUID,
    body: EventInviteCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventInviteOut]:
    """Invite people. Host or e-board only.

    ON CONFLICT DO NOTHING rather than a check-then-insert: inviting the roster twice is
    an ordinary double-tap, and the primary key is what makes the second one harmless.
    Returns the event's full invite list so the caller does not have to guess which of
    its ids were new.

    AN INVITE GRANTS READ ACCESS (see `_readable_event`), so this endpoint is a
    permission grant and is gated like one - a rank-and-file member cannot use somebody
    else's event to expose it to an outsider.
    """
    event, membership = await _event_with_membership(event_id, user, session)
    if not _may_manage(event, membership):
        raise forbidden("not_the_host")

    # Reject unknown users up front. Without this the FK raises IntegrityError and the
    # whole batch fails as a 500 for one bad id.
    known = await session.execute(
        select(models.User.id).where(models.User.id.in_(body.user_ids))
    )
    known_ids = {row for row in known.scalars().all()}
    unknown = set(body.user_ids) - known_ids
    if unknown:
        raise HTTPException(status_code=422, detail="unknown_user_in_invite_list")

    await session.execute(
        pg_insert(models.EventInvite)
        .values(
            [
                {
                    "event_id": event_id,
                    "invited_user_id": invited_id,
                    "invited_by": user.id,
                }
                for invited_id in known_ids
            ]
        )
        .on_conflict_do_nothing(constraint="pk_event_invites")
    )
    await session.commit()

    rows = await session.execute(
        select(models.EventInvite)
        .where(models.EventInvite.event_id == event_id)
        .order_by(models.EventInvite.created_at)
    )
    return [EventInviteOut.model_validate(i) for i in rows.scalars().all()]


@router.get("/events/{event_id}/guests")
async def list_guests(
    event_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventGuestsOut:
    """Who was invited and how everyone answered.

    NEVER PUBLIC, at any visibility tier. Being able to read an event does not entitle
    you to its guest list: a campus-wide or public party would otherwise publish who is
    attending it to anyone who opened the link. Membership, an invite, or an RSVP of
    your own - one of the three ways of actually being part of this event.
    """
    event = await _readable_event(event_id, user, session)

    if await _membership_in(event.chapter_id, user, session) is None:
        invited = await session.get(models.EventInvite, (event_id, user.id))
        mine = await session.get(models.EventRsvp, (event_id, user.id))
        if invited is None and mine is None:
            raise forbidden("not_on_the_guest_list")

    invite_rows = await session.execute(
        select(models.EventInvite)
        .where(models.EventInvite.event_id == event_id)
        .order_by(models.EventInvite.created_at)
    )
    rsvp_rows = await session.execute(
        select(models.EventRsvp)
        .where(models.EventRsvp.event_id == event_id)
        .order_by(models.EventRsvp.created_at)
    )
    return EventGuestsOut(
        invites=[EventInviteOut.model_validate(i) for i in invite_rows.scalars().all()],
        rsvps=[EventRsvpOut.model_validate(r) for r in rsvp_rows.scalars().all()],
    )


@router.get("/me/event-invites")
async def list_my_invites(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    """Events the caller has been invited to, soonest-first.

    Serves the invitee's side of the feature: an invite is worthless if the only way to
    find it is a link somebody sent you. Cancelled events are INCLUDED rather than
    filtered - "the thing you were invited to is off" is the single most important row
    this endpoint can return.
    """
    result = await session.execute(
        select(models.Event)
        .join(models.EventInvite, models.EventInvite.event_id == models.Event.id)
        .where(models.EventInvite.invited_user_id == user.id)
        .order_by(models.Event.starts_at.desc())
    )
    return [EventOut.model_validate(e) for e in result.scalars().all()]


@router.get("/events/{event_id}/rsvps")
async def list_rsvps(
    event_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventRsvpOut]:
    """List an event's RSVPs. Same gate as the guest list - this IS the guest list."""
    guests = await list_guests(event_id, user, session)
    return guests.rsvps


@router.put("/events/{event_id}/rsvps")
async def upsert_rsvp(
    event_id: uuid.UUID,
    body: EventRsvpUpdate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventRsvpOut:
    """Upsert the caller's RSVP (going/maybe/cant).

    Anyone who may READ the event may answer it - the read gate already encodes who
    belongs at this party, and a second, different rule for answering would drift from
    it. A cancelled event refuses new answers: the party is off, and letting the Going
    count keep climbing afterwards is just misinformation.
    """
    event = await _readable_event(event_id, user, session)
    if event.canceled_at is not None:
        raise forbidden("event_canceled")

    rsvp = await session.get(models.EventRsvp, (event_id, user.id))
    if rsvp is None:
        rsvp = models.EventRsvp(event_id=event_id, user_id=user.id, status=body.status)
        session.add(rsvp)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent double-tap raced us to insert the same (event_id, user_id)
            # RSVP row - rollback and apply the status to the row that won instead.
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


@router.get("/public/events/{event_id}")
async def get_public_event(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> PublicEventOut:
    """One event, to anyone at all. NO AUTHENTICATION.

    THE ONLY UNAUTHENTICATED ROUTE IN THIS MODULE, and the only one in the app that
    reaches chapter content without passing c88. It is safe only because of two things
    that must both stay true:

      1. It refuses anything that is not visibility == 'public'. A 404, not a 403 -
         an anonymous caller learns nothing about which ids exist.
      2. It serialises PublicEventOut, which has no host, no chapter id and no guest
         list. Returning EventOut here would publish the roster of who is attending a
         real party to the open internet.

    The going count is the one derived number, and it is a COUNT rather than names.
    """
    event = await session.get(models.Event, event_id)
    if event is None or event.visibility != "public":
        raise not_found("event_not_found")

    chapter = await session.get(models.Chapter, event.chapter_id)
    hosted_by = "A chapter"
    if chapter is not None:
        hosted_by = (
            f"{chapter.org_name} {chapter.chapter_name}"
            if chapter.chapter_name
            else chapter.org_name
        )

    going_count = await session.scalar(
        select(func.count())
        .select_from(models.EventRsvp)
        .where(
            models.EventRsvp.event_id == event_id,
            models.EventRsvp.status == "going",
        )
    )

    return PublicEventOut(
        id=event.id,
        title=event.title,
        cover_url=event.cover_url,
        description=event.description,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        location=event.location,
        canceled_at=event.canceled_at,
        hosted_by=hosted_by,
        going_count=going_count or 0,
    )
