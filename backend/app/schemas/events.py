"""Events schemas: chapter events (Partiful-style), invites and RSVPs (c33, c198)."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import validate_public_url

RsvpStatus = Literal["going", "maybe", "cant"]

# Ordered by how far the event travels, narrowest first.
#   chapter  - active members of the hosting chapter only (what events meant before c198)
#   campus   - .edu-verified students of this chapter's campus (the c88 population)
#   verified - any .edu-verified user, so a sister chapter or another school can be invited
#   public   - no account at all
EventVisibility = Literal["chapter", "campus", "verified", "public"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- events ----


class EventCreate(_Schema):
    title: str = Field(min_length=1)
    starts_at: datetime
    location: str = Field(min_length=1)
    cover_url: str = Field(min_length=1)
    description: str | None = None
    ends_at: datetime | None = None
    # DEFAULTS TO THE NARROWEST TIER. A client that omits this gets a members-only
    # event, never a world-readable one - the same reason the column's server_default is
    # 'chapter'. Widening is always an explicit act by a host.
    visibility: EventVisibility = "chapter"

    # c184 sweep: cover_url is client-supplied and written straight through to the
    # events row, the same shape of gap the card flagged in alumni.linkedin_url /
    # apply_url. http(s)-only, <= 2048 chars.
    @field_validator("cover_url")
    @classmethod
    def _validate_cover_url(cls, value: str) -> str:
        validated = validate_public_url(value)
        assert validated is not None  # field is required (min_length=1), never None
        return validated

    # A naive datetime from a client is taken as UTC, same as chapters.py and
    # core/invites.py. Without this, one naive and one aware value make the ordering
    # check below (and the tz-aware column writes) raise TypeError, which pydantic
    # does not translate into a 422 - it surfaces as a raw 500.
    @field_validator("starts_at", "ends_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _ends_after_starts(self) -> "EventCreate":
        # The CHECK constraint would also reject this, but a 422 naming the problem
        # beats an IntegrityError surfacing as a 500 to a host who fat-fingered a date.
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class EventUpdate(_Schema):
    """A host's edit. Every field optional; omitted fields are left alone.

    OMITTED VS EXPLICIT-NULL IS THE WHOLE CONTRACT HERE, and pydantic v2 already gives
    it to us for free: update_event reads body.model_dump(exclude_unset=True), and
    exclude_unset looks at model_fields_set, not at the value. A field left out of the
    JSON body entirely never enters model_fields_set and is excluded - "leave it
    alone". A field sent as explicit `null` (e.g. {"ends_at": null}) DOES enter
    model_fields_set with a value of None, survives exclude_unset, and reaches
    setattr(event, "ends_at", None) - "clear it". So {"ends_at": null} in the request
    body clears a previously-set end time (and likewise for description); leaving
    ends_at out of the body keeps whatever was stored. See test_events.py's
    test_explicit_null_clears_ends_at_and_description_omission_leaves_them (c202) for
    the proof, including that a cleared ends_at correctly skips the ends-after-starts
    check on the next edit rather than failing it.
    """

    title: str | None = Field(default=None, min_length=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: str | None = Field(default=None, min_length=1)
    cover_url: str | None = Field(default=None, min_length=1)
    description: str | None = None
    visibility: EventVisibility | None = None

    @field_validator("cover_url")
    @classmethod
    def _validate_cover_url(cls, value: str | None) -> str | None:
        return validate_public_url(value) if value is not None else None

    # Same normalization as EventCreate: update_event compares the submitted value
    # against the STORED one (always aware, the columns are timezone=True), so a naive
    # datetime here would hit the same TypeError-as-500 inside the route handler.
    @field_validator("starts_at", "ends_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class EventOut(_Schema):
    """An event as somebody who is allowed to see it reads it."""

    id: uuid.UUID
    chapter_id: uuid.UUID
    title: str
    cover_url: str
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    location: str
    visibility: EventVisibility
    canceled_at: datetime | None = None
    host_id: uuid.UUID
    created_at: datetime


class PublicEventOut(_Schema):
    """An event as an UNAUTHENTICATED reader sees it. Deliberately narrower.

    THIS IS A SAFETY BOUNDARY, NOT A CONVENIENCE. A public event is a hole through the
    c88 .edu gate that braul opened knowingly on c198, and the only thing keeping the
    hole the size it was agreed to be is that this class carries fewer fields than
    EventOut. It has NO host_id, NO chapter_id, NO guest list and NO RSVP breakdown:
    the internet gets to know a party is happening, not who is going to it.

    Anyone "simplifying" this by returning EventOut from the public route is widening a
    decision that was made with the risk written down. The going_count is the single
    number here that is derived from other people's behaviour, and it is a count rather
    than names on purpose.
    """

    id: uuid.UUID
    title: str
    cover_url: str
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    location: str
    canceled_at: datetime | None = None
    # Display name of the hosting chapter ("Sigma Chi Epsilon Mu"), not its id - a
    # public reader has no business enumerating chapter ids.
    hosted_by: str
    going_count: int


# ---- invites ----


class EventInviteCreate(_Schema):
    """Invite people to an event. Idempotent per person by construction.

    A list rather than one id per request: inviting the roster is the ordinary case, and
    one request per member would be N round trips and N chances to half-finish.
    """

    user_ids: list[uuid.UUID] = Field(min_length=1)


class EventInviteOut(_Schema):
    event_id: uuid.UUID
    invited_user_id: uuid.UUID
    invited_by: uuid.UUID
    created_at: datetime


# ---- rsvps ----


class EventRsvpUpdate(_Schema):
    status: RsvpStatus


class EventRsvpOut(_Schema):
    event_id: uuid.UUID
    user_id: uuid.UUID
    status: RsvpStatus
    created_at: datetime


class EventWithRsvpsOut(_Schema):
    """One row of GET /chapters/{id}/events-with-rsvps (c43): an event plus all its
    RSVPs, so the Events segment renders in one round trip instead of 1+N."""

    event: EventOut
    rsvps: list[EventRsvpOut]


class EventInviteWithRsvpOut(_Schema):
    """One row of GET /me/event-invites-with-rsvps (c204): an invited event, plus the
    two things the mobile client used to fetch per-invite - the same shape of fix c43's
    EventWithRsvpsOut applied to a chapter's own events, applied here to a cross-chapter
    invite list.

    my_rsvp_status is the CALLER'S OWN answer, never anyone else's - null means they
    have not answered. hosted_by is the hosting chapter's display name ("{org_name}
    {chapter_name}"), safe to show a non-member because an invite already admits them
    to the event, which shows who is hosting it. It is never a placeholder like the
    client's old "Another chapter" fallback: chapter_id is a NOT NULL FK on events, so
    a joined query always resolves a real chapter.
    """

    event: EventOut
    my_rsvp_status: RsvpStatus | None
    hosted_by: str


class EventGuestsOut(_Schema):
    """The guest list: who was invited, and how everyone answered.

    Invites and RSVPs are returned side by side rather than merged, because the screen
    has to tell them apart - "invited, has not answered" is a different row from "said
    no", and a merged list cannot express someone who RSVPd without an invite (which a
    public or campus-wide event produces constantly).
    """

    invites: list[EventInviteOut]
    rsvps: list[EventRsvpOut]
