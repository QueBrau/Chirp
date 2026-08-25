"""Touse/Bouse weekly house leaderboard (board card c175).

One row per (campus, week, voter). See migration 0020 for the full reasoning; the
short version is that every constraint worth having here is in the SCHEMA rather than
in a route:

  * one ballot per student per week is the PRIMARY KEY, not a check-then-insert
  * a ballot cannot name a chapter from another campus - composite FK on
    (chapter_id, campus_id), because two cross-campus leaks have already shipped in
    this repo and both passed every single-campus test
  * the same house cannot be both Touse and Bouse - CHECK constraint
  * bouse_chapter_id is NULLABLE on purpose: a voter may name a top house without
    naming a bottom one, and forcing them would manufacture Bouse signal

Ballots are stored raw and every standing is derived at read time. That is what keeps
the "how public is the bottom of the ranking" decision (c175) reversible without a
migration.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class HouseBallot(Base):
    __tablename__ = "house_ballots"
    __table_args__ = (
        PrimaryKeyConstraint("campus_id", "week_start", "voter_id", name="pk_house_ballots"),
        ForeignKeyConstraint(
            ["touse_chapter_id", "campus_id"],
            ["chapters.id", "chapters.campus_id"],
            name="fk_house_ballots_touse_same_campus",
        ),
        ForeignKeyConstraint(
            ["bouse_chapter_id", "campus_id"],
            ["chapters.id", "chapters.campus_id"],
            name="fk_house_ballots_bouse_same_campus",
        ),
        CheckConstraint(
            "bouse_chapter_id IS NULL OR bouse_chapter_id <> touse_chapter_id",
            name="ck_house_ballots_distinct_houses",
        ),
    )

    campus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id"), nullable=False
    )
    # The Monday of the voting week, computed server-side. NEVER accepted from a
    # client: a client-supplied week lets anyone stuff a past week after seeing how it
    # went, and would leave nothing in the data to show they had.
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    # Who voted. Stored so abuse can be investigated, and NEVER serialized to any
    # client - the same rule Yak.author_id follows (SPEC 8.3).
    voter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    touse_chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    bouse_chapter_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
