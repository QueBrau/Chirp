"""Messaging models (ciphertext only): conversations, members, messages, receipts (SPEC §3)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Stands in for a NULL chapter_id inside Conversation.dm_key (board c344). Postgres
# treats NULLs as pairwise-distinct in a UNIQUE index, so a plain
# f"{chapter_id}:..." key built straight from a NULL chapter_id would never actually
# dedupe the (more common) chapterless-DM case — every row would look unique to the
# index even when the participant pair repeats. This sentinel gives that case a real,
# comparable value instead. See migration 0037 for the partial unique index this backs.
NIL_UUID = uuid.UUID(int=0)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("kind IN ('dm','group')", name="ck_conversations_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # NULL for cross-chapter DMs.
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Group name (plaintext metadata, OK).
    title: Mapped[str | None] = mapped_column(Text)
    # 2 = E2EE from day one.
    protocol_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("2")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Canonical DM identity (board c344): f"{chapter_id or NIL_UUID}:{sorted user id
    # pair}", computed ONLY for kind="dm" conversations with exactly two members —
    # NULL for everything else (groups, and any malformed multi-recipient "dm" row,
    # unvalidated today and out of scope here). Backed by a partial UNIQUE index
    # WHERE dm_key IS NOT NULL added in migration 0037, which also carries the
    # duplicate-safe backfill for rows created before this column existed. See
    # routers/messages.py's _dm_key for exactly how the string is built.
    dm_key: Mapped[str | None] = mapped_column(Text)


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (
        # The primary key is (conversation_id, user_id) - conversation_id leads, so its
        # index cannot serve a lookup keyed on user_id alone. list_conversations
        # (routers/messages.py) is the one reader that filters this table by user_id -
        # `WHERE conversation_members.user_id = :user_id AND left_at IS NULL` - to build
        # a user's inbox, and until this index existed that predicate had no choice but
        # to scan every row in the table. Partial on `left_at IS NULL`, matching that
        # filter exactly (c208's post_comments precedent: index the predicate readers
        # actually use, not a plain column). Board card c212.
        Index(
            "idx_conversation_members_user_active",
            "user_id",
            postgresql_where=text("left_at IS NULL"),
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Triggers sender-key rotation client-side.
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "idx_messages_convo_time",
            "conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    sender_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False
    )
    # Server NEVER parses this (SPEC §8.1).
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # signal | sender_key_distribution
    message_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'signal'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MessageReceipt(Base):
    __tablename__ = "message_receipts"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), primary_key=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), primary_key=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
