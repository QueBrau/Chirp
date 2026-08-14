"""Events schemas: chapter events (Partiful-style) and RSVPs."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RsvpStatus = Literal["going", "maybe", "cant"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- events ----


class EventCreate(_Schema):
    title: str = Field(min_length=1)
    date_label: str = Field(min_length=1)
    location: str = Field(min_length=1)
    cover_url: str = Field(min_length=1)


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
