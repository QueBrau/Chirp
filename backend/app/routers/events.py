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

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.analytics import emit
from app.core.blocks import blockers_of
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
    EventInviteWithRsvpOut,
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
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    """List the chapter's events, soonest-first by start time.

    Board card c201 (same bug class as c127 / SECURITY-REVIEW finding 10): this had no
    limit at all, so a chapter with enough event history returned its entire events
    table on every load. Cursor-paginated on (starts_at, id) - `before` + `before_id` -
    the same compound-cursor shape as chirps.py's list_chirps and messages.py's
    list_messages, so a page boundary landing on two events with an identical
    starts_at never silently drops one of them. `before` alone still works (legacy
    clients) but does not guarantee that tie-break.

    Mobile does not send these params yet - out of scope for c201 - so every existing
    caller keeps getting the same (now capped) first page it always did.
    """
    stmt = select(models.Event).where(models.Event.chapter_id == chapter_id)
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Event.starts_at, models.Event.id) < (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Event.starts_at < before)
    stmt = stmt.order_by(models.Event.starts_at.desc(), models.Event.id.desc()).limit(limit)

    result = await session.execute(stmt)
    return [EventOut.model_validate(e) for e in result.scalars().all()]


@router.get("/chapters/{chapter_id}/events-with-rsvps")
async def list_events_with_rsvps(
    chapter_id: uuid.UUID,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[EventWithRsvpsOut]:
    """The chapter's events with all their RSVPs (c43).

    Collapses the Events segment's 1+N (listEvents + listRsvps per event) into two
    queries total: one for the events, one IN-clause for their RSVPs.

    Same (starts_at, id) cursor as list_events (c201) - `before` + `before_id`, capped
    `limit`. The RSVP IN-query below fans out over whatever page of events comes back
    from THIS query, so bounding the event page bounds the RSVP query as a
    consequence; it does not need (and must not grow) a second cursor of its own.
    """
    stmt = select(models.Event).where(models.Event.chapter_id == chapter_id)
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Event.starts_at, models.Event.id) < (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Event.starts_at < before)
    stmt = stmt.order_by(models.Event.starts_at.desc(), models.Event.id.desc()).limit(limit)

    result = await session.execute(stmt)
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
    emit(
        "event_created",
        user_id=membership.user_id,
        chapter_id=chapter_id,
        event_id=event.id,
        visibility=event.visibility,
    )
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

    # An invite is contact: it puts this event in the invitee's list_my_invites and grants
    # them read access, so someone who blocked the inviter must not receive one (board
    # card c243 - blocks used to be enforced on read paths only, which let a blocked user
    # keep reaching people through events). Dropped SILENTLY rather than refused: a 403
    # naming the ids that were skipped would tell the inviter exactly who blocked them,
    # and inviting a roster is a bulk action where one blocked member must not fail the
    # other fifty. The blocked-by user simply is not invited.
    blockers = await blockers_of(session, subject_id=user.id, candidate_ids=known_ids)
    invitable_ids = known_ids - blockers

    if invitable_ids:
        await session.execute(
            pg_insert(models.EventInvite)
            .values(
                [
                    {
                        "event_id": event_id,
                        "invited_user_id": invited_id,
                        "invited_by": user.id,
                    }
                    for invited_id in invitable_ids
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
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    """Events the caller has been invited to, soonest-first.

    Serves the invitee's side of the feature: an invite is worthless if the only way to
    find it is a link somebody sent you. Cancelled events are INCLUDED rather than
    filtered - "the thing you were invited to is off" is the single most important row
    this endpoint can return.

    SOONEST-FIRST HERE MEANS ASCENDING starts_at - the opposite direction from every
    other list route in this module, which are reverse-chron (furthest-in-future
    first). Fixed alongside c201 pagination: this docstring already said soonest-first,
    but the query sorted descending before this change, so an invitee's nearest
    upcoming event was buried at the bottom instead of surfaced at the top. The cursor
    direction has to match the sort it's paginating: continuing past the last row of a
    page here means moving to a LATER starts_at, so the compound comparison is
    `> (before, before_id)`, not `<` like every DESC route in this file.
    """
    stmt = (
        select(models.Event)
        .join(models.EventInvite, models.EventInvite.event_id == models.Event.id)
        .where(models.EventInvite.invited_user_id == user.id)
    )
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Event.starts_at, models.Event.id) > (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Event.starts_at > before)
    stmt = stmt.order_by(models.Event.starts_at.asc(), models.Event.id.asc()).limit(limit)

    result = await session.execute(stmt)
    return [EventOut.model_validate(e) for e in result.scalars().all()]


@router.get("/me/event-invites-with-rsvps")
async def list_my_invites_with_rsvps(
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventInviteWithRsvpOut]:
    """The bulk sibling of list_my_invites (c204): same rows, plus the caller's own RSVP
    status and a hosting-chapter label, in one joined query instead of two per-invite
    round trips the mobile client used to make itself.

    Those two round trips were: listGuests(event.id) to learn whether the caller had
    already answered - the N+1 flagged in c203's review, one call per invite - and
    GET /chapters/{id} to name the hosting chapter, which 404s for a cross-chapter
    invitee because that route is member-scoped (chapters.py get_chapter), so the
    client fell back to a bare "Another chapter". Both gaps close here: an OUTER join to
    EventRsvp scoped to (event_id, user.id) - not just event_id - so only the CALLER'S
    OWN status can ever come back, never another invitee's, and an INNER join to
    Chapter for the label, safe for a non-member to read because an invite already
    admits them to the event, which shows who is hosting it. Exposing the display name
    this way is not exposing the chapter id: EventOut already carries chapter_id, and
    that is the only chapter identifier this route or EventOut hands back.

    Same soonest-first ASCENDING order and (starts_at, id) cursor contract as
    list_my_invites - see that docstring for why ascending is correct here and not the
    DESC every other list route in this file uses. list_my_invites itself is untouched;
    this is an additive route, not a replacement.
    """
    stmt = (
        select(models.Event, models.Chapter, models.EventRsvp)
        .join(models.EventInvite, models.EventInvite.event_id == models.Event.id)
        .join(models.Chapter, models.Chapter.id == models.Event.chapter_id)
        .outerjoin(
            models.EventRsvp,
            (models.EventRsvp.event_id == models.Event.id)
            & (models.EventRsvp.user_id == user.id),
        )
        .where(models.EventInvite.invited_user_id == user.id)
    )
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Event.starts_at, models.Event.id) > (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Event.starts_at > before)
    stmt = stmt.order_by(models.Event.starts_at.asc(), models.Event.id.asc()).limit(limit)

    result = await session.execute(stmt)
    return [
        EventInviteWithRsvpOut(
            event=EventOut.model_validate(event),
            my_rsvp_status=rsvp.status if rsvp is not None else None,
            hosted_by=(
                f"{chapter.org_name} {chapter.chapter_name}"
                if chapter.chapter_name
                else chapter.org_name
            ),
        )
        for event, chapter, rsvp in result.all()
    ]


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
    emit(
        "event_rsvp",
        user_id=user.id,
        chapter_id=event.chapter_id,
        event_id=event_id,
        status=rsvp.status,
    )
    return EventRsvpOut.model_validate(rsvp)


# How long a LIVE public event may be served from a cache. Short, and the number is a
# SAFETY decision rather than a performance one (board c218): the dangerous staleness is
# serving "the party is on" after it was called off, which sends people to an address.
# Sixty seconds bounds that. A going_count sixty seconds behind is harmless by
# comparison, which is why the count does not get a say in this number.
PUBLIC_EVENT_MAX_AGE_SECONDS = 60
# A CANCELED event may be cached far longer, because cancellation is terminal - there is
# no transition back to "on", so the direction that could hurt someone does not exist.
PUBLIC_EVENT_CANCELED_MAX_AGE_SECONDS = 3600


@router.get("/public/events/{event_id}")
async def get_public_event(
    event_id: uuid.UUID,
    response: Response,
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

    # `public`, not `private`: letting SHARED caches serve this is the entire point.
    # This route exists to be pasted into a group chat, so one popular party is
    # thousands of requests for one id, and every one of them currently costs three
    # Postgres round trips (event, chapter, going count) on the scarcest resource in the
    # system. Nothing here varies by caller - the route is unauthenticated and
    # PublicEventOut carries no viewer-specific field - so a shared cache is safe in a
    # way it is not for any other read in this app.
    #
    # THIS HEADER IS THE PRECONDITION FOR A CDN, not a replacement for one. A CDN put in
    # front of Cloud Run today would cache nothing, because nothing in the response
    # tells it that it may.
    max_age = (
        PUBLIC_EVENT_CANCELED_MAX_AGE_SECONDS
        if event.canceled_at is not None
        else PUBLIC_EVENT_MAX_AGE_SECONDS
    )
    response.headers["Cache-Control"] = f"public, max-age={max_age}"

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
