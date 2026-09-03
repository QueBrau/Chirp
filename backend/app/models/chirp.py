"""Chirp (anonymous board) models: chirps, votes, content reports, user blocks (SPEC §3)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Chirp(Base):
    __tablename__ = "chirps"
    __table_args__ = (
        Index(
            "idx_chirps_campus_time",
            "campus_id",
            text("created_at DESC"),
            postgresql_where=text("removed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    campus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id"), nullable=False
    )
    # NEVER exposed via API (SPEC §8.3).
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_reason: Mapped[str | None] = mapped_column(Text)


class ChirpVote(Base):
    __tablename__ = "chirp_votes"
    __table_args__ = (
        CheckConstraint("value IN (-1, 1)", name="ck_chirp_votes_value"),
    )

    chirp_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chirps.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    value: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class ContentReport(Base):
    __tablename__ = "content_reports"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('chirp','post','comment','message_forward','user')",
            name="ck_content_reports_target_type",
        ),
        CheckConstraint(
            "status IN ('open','actioned','dismissed')",
            name="ck_content_reports_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    reporter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # Resolved server-side from the target (finding 1: moderation scoping); NULL only when
    # the target's campus could not be determined (best-effort fallback exhausted).
    campus_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id")
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # For E2EE message reports: client forwards plaintext.
    forwarded_plaintext: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class UserBlock(Base):
    __tablename__ = "user_blocks"
    __table_args__ = (
        # c237, migration 0029. Blocking yourself is not a moderation setting: feed.py's
        # c35 anti-join hides posts whose author the caller has blocked and does not
        # exempt the caller, so such a row takes the user's own posts off their own
        # feed. The route refuses it (403 cannot_block_self at both block endpoints);
        # this is the same rule where it cannot be routed around.
        CheckConstraint("blocker_id <> blocked_id", name="ck_user_blocks_no_self_block"),
    )

    blocker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    blocked_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # c279, migration 0030. WHY the block was created, because it decides WHAT it hides.
    #
    #   'named'    - the caller chose a person they can see. Hides everything, exactly as
    #                every block did before this column existed.
    #   'by_chirp' - the caller blocked an anonymous chirp's author without learning who
    #                that is. Hides CHIRP SURFACES ONLY.
    #
    # The distinction is the whole of c279: a by-chirp block that ALSO hid the author's
    # named posts let a feed diff before/after the block name them - the exact identity
    # POST /moderation/blocks/by-chirp refuses to return. Contact enforcement
    # (app.core.blocks) deliberately ignores this column and refuses BOTH kinds; see that
    # module's docstring for why that is not a probe channel.
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'named'")
    )
