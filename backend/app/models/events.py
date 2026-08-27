"""Events models: chapter events (Partiful-style), invites and RSVPs (c33, c198).

VISIBILITY AND INVITES ARE TWO DIFFERENT AXES and the distinction is the whole design:

    Event.visibility - who can FIND the event without being invited
    EventInvite      - an explicit grant admitting one named person regardless of tier

Both grant read access; they differ in whether the host had to name you. So a 'campus'
event can still be shown to one alum or a sister chapter by inviting them, and a
'public' event still has a real invite list of the people actually told about it.

Folding the two into one column would make "invite one specific person to an event the
rest of the campus can also see" unrepresentable, which is the ordinary case. Making an
invite NOT grant access would be worse still: the invite button would do nothing for
precisely the people a host most wants to invite. The enforcement lives in
routers/events.py::_readable_event.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('chapter','campus','verified','public')",
            name="ck_events_visibility",
        ),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_events_ends_after_starts",
        ),
        Index(
            "idx_events_chapter_starts",
            "chapter_id",
            text("starts_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    cover_url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # A real instant, server-stored in UTC. Replaced the free-text date_label in 0024 -
    # sorting upcoming from past, reminders and calendar export all need an instant, and
    # a string next to a timestamp is two answers to "when is the party".
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Nullable: plenty of parties have no stated end, and inventing one would put a
    # wrong time on screen rather than no time.
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location: Mapped[str] = mapped_column(Text, nullable=False)
    # 'chapter' | 'campus' | 'verified' | 'public', narrowest first. DEFAULT IS THE
    # NARROWEST on purpose, twice over: a code path that forgets to set it produces a
    # members-only event rather than a world-readable one, and every row predating 0024
    # keeps the chapter-only semantics it was created under. See c198.
    visibility: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'chapter'")
    )
    # NULL == live. A canceled event keeps its row and its guest list so it can render
    # as "Canceled" to the people who RSVPd - deleting it would remove it from the
    # screens of exactly the people who needed telling.
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EventInvite(Base):
    """One person the host explicitly told about an event, and thereby admitted to it.

    Distinct from an RSVP: an invite is what the HOST did, an RSVP is what the GUEST
    did. Both can exist without the other - somebody can RSVP to a public event nobody
    invited them to, and an invited person can never answer.

    Creating a row here GRANTS READ ACCESS to the event, so the endpoint that writes it
    is gated as the permission grant it is (host or e-board), not as an ordinary write.
    """

    __tablename__ = "event_invites"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    invited_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EventRsvp(Base):
    __tablename__ = "event_rsvps"
    __table_args__ = (
        CheckConstraint(
            "status IN ('going','maybe','cant')",
            name="ck_event_rsvps_status",
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
