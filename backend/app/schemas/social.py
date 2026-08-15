"""Social feed schemas: posts, likes, comments."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Audience = Literal["org", "campus"]
PostType = Literal["text", "photo", "video"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- posts ----


class PostCreate(_Schema):
    body: str = Field(min_length=1)
    media_urls: list[str] | None = None
    # Author-chosen at compose time; defaults to 'org' so a client that omits this
    # field can never accidentally broadcast a chapter post campus-wide (board
    # Decisions log, Aug 14).
    audience: Audience = "org"
    post_type: PostType = "text"
    duration_sec: int | None = None  # video posts only


class PostUpdate(_Schema):
    body: str | None = None
    media_urls: list[str] | None = None


class PostOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    media_urls: list[str] | None = None
    audience: Audience
    post_type: PostType
    duration_sec: int | None = None
    created_at: datetime
    deleted_at: datetime | None = None


class FeedPostOut(_Schema):
    """PostOut fields plus the author's display identity and batched engagement
    counts (c43): like_count, comment_count, liked_by_me, in one round trip.

    display_name is non-null: the query INNER JOINs users on posts.author_id,
    matching the MemberOut precedent in schemas/identity.py.
    """

    id: uuid.UUID
    chapter_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    media_urls: list[str] | None = None
    audience: Audience
    post_type: PostType
    duration_sec: int | None = None
    created_at: datetime
    display_name: str
    avatar_url: str | None = None
    like_count: int
    comment_count: int
    liked_by_me: bool


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
