"""Events schemas: chapter events (Partiful-style) and RSVPs."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validation import validate_public_url

RsvpStatus = Literal["going", "maybe", "cant"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- events ----


class EventCreate(_Schema):
    title: str = Field(min_length=1)
    date_label: str = Field(min_length=1)
    location: str = Field(min_length=1)
    cover_url: str = Field(min_length=1)

    # c184 sweep: cover_url is client-supplied and written straight through to
    # the events row with no validation (routers/events.py create_event), the
    # same shape of gap the card flagged in alumni.linkedin_url / apply_url.
    # http(s)-only, <= 2048 chars.
    @field_validator("cover_url")
    @classmethod
    def _validate_cover_url(cls, value: str) -> str:
        validated = validate_public_url(value)
        assert validated is not None  # field is required (min_length=1), never None
        return validated


class EventOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    title: str
    cover_url: str
    date_label: str
    location: str
    host_id: uuid.UUID
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
