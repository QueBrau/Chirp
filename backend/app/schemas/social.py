"""Social feed schemas: posts, likes, comments."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- posts ----


class PostCreate(_Schema):
    body: str = Field(min_length=1)
    media_urls: list[str] | None = None


class PostUpdate(_Schema):
    body: str | None = None
    media_urls: list[str] | None = None


class PostOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    media_urls: list[str] | None = None
    created_at: datetime
    deleted_at: datetime | None = None


# ---- likes ----


class PostLikeOut(_Schema):
    post_id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime


# ---- comments ----


class PostCommentCreate(_Schema):
    body: str = Field(min_length=1)


class PostCommentOut(_Schema):
    id: uuid.UUID
    post_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    created_at: datetime
    deleted_at: datetime | None = None
