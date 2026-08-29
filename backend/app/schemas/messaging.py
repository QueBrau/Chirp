"""Messaging schemas (ciphertext only): conversations, members, messages, receipts."""

import base64
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BeforeValidator, Field

from app.schemas.base import _Schema

ConversationKind = Literal["dm", "group"]
MessageType = Literal["signal", "sender_key_distribution"]


def _to_b64(value: object) -> object:
    """Encode raw bytes (from ORM BYTEA columns) to a base64 str; pass strings through."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


Base64Str = Annotated[str, BeforeValidator(_to_b64)]


# ---- conversations ----


class ConversationCreate(_Schema):
    """Body for POST /conversations — member_user_ids are the other participants."""

    chapter_id: uuid.UUID | None = None
    kind: ConversationKind
    title: str | None = None
    member_user_ids: list[uuid.UUID] = Field(default_factory=list)


class ConversationMemberOut(_Schema):
    conversation_id: uuid.UUID
    user_id: uuid.UUID
    joined_at: datetime
    left_at: datetime | None = None


class ConversationOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID | None = None
    kind: ConversationKind
    title: str | None = None
    protocol_version: int
    created_at: datetime
    members: list[ConversationMemberOut] | None = None


# ---- messages ----


class MessageCreate(_Schema):
    """Ciphertext in — the server never parses ciphertext_b64 (SPEC §8.1)."""

    sender_device_id: uuid.UUID
    ciphertext_b64: str = Field(min_length=1)
    message_type: MessageType = "signal"


class MessageOut(_Schema):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_device_id: uuid.UUID
    ciphertext_b64: Base64Str = Field(
        validation_alias=AliasChoices("ciphertext_b64", "ciphertext")
    )
    message_type: MessageType
    created_at: datetime


# ---- receipts ----


class MessageReceiptCreate(_Schema):
    """Body for POST /messages/{message_id}/receipts."""

    device_id: uuid.UUID
    delivered_at: datetime | None = None


class MessageReceiptOut(_Schema):
    message_id: uuid.UUID
    device_id: uuid.UUID
    delivered_at: datetime | None = None
