"""Social feed schemas: posts, likes, comments."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.validation import MAX_COMMENT_BODY_LENGTH, MAX_POST_BODY_LENGTH
from app.schemas.base import _Schema

Audience = Literal["org", "campus", "org_actives"]

PostType = Literal["text", "photo", "video"]


# ---- posts ----


class PostCreate(_Schema):
    body: str = Field(min_length=1, max_length=MAX_POST_BODY_LENGTH)
    # tmp/ object_name(s) from POST /media/upload-url (c132) - NOT a url. The route
    # moves the referenced tmp/ object to its permanent location and assigns the
    # resulting url itself; media_urls is never accepted as client input anywhere.
    media_object_names: list[str] | None = None
    # Author-chosen at compose time; defaults to 'org' so a client that omits this
    # field can never accidentally broadcast a chapter post campus-wide (board
    # Decisions log, Aug 14). 'org_actives' (c102) is reachable here too - this
    # route already requires an ACTIVE membership to reach at all (get_current_membership
    # in routers/feed.py), so there is no caller who could set it who is not
    # themselves already in the tier it names.
    audience: Audience = "org"
    post_type: PostType = "text"
    duration_sec: int | None = None  # video posts only


class CampusPostCreate(_Schema):
    """Body for POST /campuses/{campus_id}/posts — the chapter-less path (c71).

    Deliberately has NO `audience` field. A caller on this route may not have a
    chapter at all, and 'org' means "private to a chapter", so the only audience
    reachable here is 'campus' and the route hard-codes it. Omitting the field
    beats accepting one and 422-ing on 'org': there is no request a client could
    send here that should produce an org post, so the type says so.
    """

    body: str = Field(min_length=1, max_length=MAX_POST_BODY_LENGTH)
    media_object_names: list[str] | None = None  # see PostCreate.media_object_names
    post_type: PostType = "text"
    duration_sec: int | None = None  # video posts only


class PostUpdate(_Schema):
    # Capped for the same reason PostCreate.body is, and NOT reachable from that one:
    # an edit writes the same column, so a ceiling only on create would leave the
    # whole gap open behind a PATCH. (Left as `str | None` with no min_length, which
    # is a separate pre-existing inconsistency with PostCreate — an edit to "" is
    # accepted here and rejected there. Out of scope for c245, not a decision.)
    body: str | None = Field(default=None, max_length=MAX_POST_BODY_LENGTH)
    media_object_names: list[str] | None = None  # see PostCreate.media_object_names


class PostOut(_Schema):
    id: uuid.UUID
    # NULL when a chapter-less student authored this (c71); always set on an org
    # post, which the ck_posts_org_requires_chapter constraint enforces.
    chapter_id: uuid.UUID | None = None
    campus_id: uuid.UUID
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
    chapter_id: uuid.UUID | None = None
    campus_id: uuid.UUID
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


class MyPostCountOut(_Schema):
    """Response for GET /chapters/{chapter_id}/posts/count (board c217).

    Deliberately carries no user_id: the number is the CALLER'S own, taken from
    the auth dependency, and echoing whose it is would invite a client to start
    passing one. See the route's docstring for the whole argument.
    """

    chapter_id: uuid.UUID
    count: int


# ---- likes ----


class PostLikeOut(_Schema):
    post_id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime


# ---- comments ----


class PostCommentCreate(_Schema):
    body: str = Field(min_length=1, max_length=MAX_COMMENT_BODY_LENGTH)


class PostCommentOut(_Schema):
    """A comment plus the author's display identity (c228).

    The identity fields are not decoration. Before them this shape carried an
    author_id and nothing else, and there is no route anywhere in the API that turns
    a user id into a person — no GET /users/{id}, and the chapter roster only covers
    members of a chapter the caller belongs to. So a client rendering a comment
    thread from the old shape had exactly one thing it could print: the raw UUID.

    Joined server-side for the same reason FeedPostOut carries them (c43): one query
    for the thread, not one request per comment author.

    display_name is non-null, matching FeedPostOut/MemberOut. Both producers can
    promise that — list_comments INNER JOINs users on author_id, and create_comment
    already holds the authenticated author it is writing.
    """

    id: uuid.UUID
    post_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    created_at: datetime
    deleted_at: datetime | None = None
    display_name: str
    avatar_url: str | None = None
