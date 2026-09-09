"""Durable delivery outbox (board card c356).

One row per fan-out event that must survive a crash between the domain write commit
and the actual publish (send_message's WS/push loop today; poll delivery is deferred,
see DELIVERY-OUTBOX.md and the "NOT WIRED" note below). The row is written in the SAME
transaction as the domain write it backs (app/services/outbox.py's `enqueue`), so a
rollback of one rolls back the other.

kind is checked at the database (migration 0039) to only ever be 'message' or 'poll',
even though this PR only ever inserts 'message' rows -- 'poll' stays reserved for the
named follow-up so the column does not need a second migration to widen it later.

recipient_ids is the set that STILL needs delivery: it starts as every intended
recipient and narrows to the failed subset after a partial success (never widens).
payload never carries ciphertext or any credential -- it is JSON safe to log in full;
see app/services/outbox.py for exactly what each kind's payload holds.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DeliveryOutbox(Base):
    __tablename__ = "delivery_outbox"
    __table_args__ = (
        CheckConstraint("kind IN ('message', 'poll')", name="ck_delivery_outbox_kind"),
        Index(
            "idx_delivery_outbox_pending",
            "next_attempt_at",
            postgresql_where=text("delivered_at IS NULL AND dead_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # ARRAY(UUID), not JSONB: this repo's only list-column precedent is ARRAY(Text)
    # (Campus.email_domains, Post.media_urls) and JSONB here is reserved for
    # dict-shaped payloads (finance.py's expected/observed) -- see plan-c356 decision 1.
    recipient_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set once, never cleared. A dead row is never retried again by the sweeper;
    # visibility only (decision 2: delivered rows are deleted, dead rows are kept).
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A short code only -- never ciphertext, never raw exception text (it could carry
    # a transport credential). See app/services/outbox.py for the fixed vocabulary.
    last_error: Mapped[str | None] = mapped_column(Text)
