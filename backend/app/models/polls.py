"""Secretary models: live polls (board card c162).

BALLOT SECRECY IS A SCHEMA DECISION, NOT A VIEW DECISION. `poll_votes` stores
`user_id` because two things genuinely need it -- enforcing one vote per member,
and telling a member what they themselves picked -- but NOTHING may report who
voted for what. That mirrors the Yak rule in SPEC section 2: anonymous to peers,
pseudonymous to the server. A chapter voting on money or on people is exactly the
case where "who voted against this" must not be answerable from the app.

The read side is aggregate-only by construction (see schemas/polls.py and
routers/polls.py) so adding a "who voted" response would take a deliberate new
query, not a careless `.options(selectinload(...))` on an existing one.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Poll(Base):
    __tablename__ = "polls"
    __table_args__ = (
        CheckConstraint("status IN ('open','closed')", name="ck_polls_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False, index=True
    )
    # A poll usually belongs to a meeting, but not always -- a snap vote between
    # meetings is the same object. Nullable rather than a second table.
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="SET NULL"), index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    options: Mapped[list["PollOption"]] = relationship(
        back_populates="poll",
        cascade="all, delete-orphan",
        order_by="PollOption.position",
    )


class PollOption(Base):
    __tablename__ = "poll_options"
    __table_args__ = (
        UniqueConstraint("poll_id", "position", name="uq_poll_options_poll_position"),
        # Not redundant with the primary key: it is the target of the composite
        # foreign key on poll_votes below, which is what makes "vote for another
        # poll's option" unrepresentable rather than merely unvalidated.
        UniqueConstraint("id", "poll_id", name="uq_poll_options_id_poll"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    poll_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("polls.id", ondelete="CASCADE"), nullable=False
    )
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    poll: Mapped[Poll] = relationship(back_populates="options")


class PollVote(Base):
    """One row per member per poll. Changing a vote UPDATES this row.

    The primary key is (poll_id, user_id), so one-vote-per-member is a database
    guarantee rather than something every future caller has to remember. A second
    ballot cannot be inserted even by a racing request.
    """

    __tablename__ = "poll_votes"
    __table_args__ = (
        # Ties the chosen option to THIS poll. Without it a member could vote in
        # poll A using an option belonging to poll B, and the tally would silently
        # count a ballot nobody could explain.
        ForeignKeyConstraint(
            ["option_id", "poll_id"],
            ["poll_options.id", "poll_options.poll_id"],
            name="fk_poll_votes_option_in_poll",
            ondelete="CASCADE",
        ),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    option_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
