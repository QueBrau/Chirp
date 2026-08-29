"""Secretary schemas: live polls (board card c162).

Every read shape here is AGGREGATE ONLY. There is deliberately no schema anywhere
in this file that carries a voter identity, so "who voted for what" cannot be
returned by filling in an existing response model -- it would take a new one, and
writing it should feel like the decision it is. See models/polls.py.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.base import _Schema

PollStatus = Literal["open", "closed"]

MAX_OPTIONS = 10


class PollCreate(_Schema):
    question: str = Field(min_length=1, max_length=500)
    # Two is the floor because a one-option poll is not a question, and the UI
    # would render a vote with no alternative as if it were a real choice.
    options: list[str] = Field(min_length=2, max_length=MAX_OPTIONS)
    meeting_id: uuid.UUID | None = None

    @field_validator("options")
    @classmethod
    def _clean_options(cls, raw: list[str]) -> list[str]:
        cleaned = [opt.strip() for opt in raw]
        if any(len(opt) == 0 for opt in cleaned):
            raise ValueError("option_text_required")
        # Case-insensitive, because two options that read identically on screen
        # split the vote and look like a bug in the tally rather than a bad poll.
        if len({opt.casefold() for opt in cleaned}) != len(cleaned):
            raise ValueError("duplicate_options")
        return cleaned


class PollVoteIn(_Schema):
    option_id: uuid.UUID


class PollOptionResult(_Schema):
    id: uuid.UUID
    text: str
    position: int
    # Aggregate. Never a list of voters.
    votes: int


class PollOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    meeting_id: uuid.UUID | None
    question: str
    status: PollStatus
    created_by: uuid.UUID
    created_at: datetime
    closed_at: datetime | None
    options: list[PollOptionResult]
    total_votes: int
    # What the CALLER picked, or null. The one identity-bearing field in the file,
    # and it only ever describes the person asking -- never anyone else.
    my_option_id: uuid.UUID | None
