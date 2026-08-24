"""Social feed models: posts, likes, comments (SPEC §3)."""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        # 'org_actives' added by migration 0019 (board c102): a third value layered
        # ON TOP of 'org' rather than a fourth privacy dimension - it is still
        # chapter-scoped exactly like 'org' (ck_posts_org_requires_chapter below
        # still applies, since its OR clause only exempts 'campus'), just visible to
        # a narrower slice of the same chapter. See routers/feed.py list_posts for
        # the actual gate.
        CheckConstraint(
            "audience IN ('org', 'campus', 'org_actives')",
            name="ck_posts_audience",
        ),
        CheckConstraint(
            "post_type IN ('text', 'photo', 'video')",
            name="ck_posts_post_type",
        ),
        # An 'org' post is BY DEFINITION private to a chapter, so it cannot exist
        # without one. Enforced here rather than only in the router because it is
        # the load-bearing half of the c71 privacy argument: chapter_id had to go
        # nullable so a chapter-less student can post to their campus, and this is
        # what stops that nullability from also inventing an org post belonging to
        # no org (which no membership check could then scope).
        CheckConstraint(
            "audience = 'campus' OR chapter_id IS NOT NULL",
            name="ck_posts_org_requires_chapter",
        ),
        Index(
            "idx_posts_chapter_time",
            "chapter_id",
            text("created_at DESC"),
        ),
        Index(
            "idx_posts_audience_time",
            "audience",
            text("created_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Serves the campus feed's exact predicate (campus + audience + live),
        # which since c71 filters posts.campus_id directly instead of joining
        # chapters, so the old chapter-shaped indexes no longer cover it.
        Index(
            "idx_posts_campus_time",
            "campus_id",
            text("created_at DESC"),
            postgresql_where=text("deleted_at IS NULL AND audience = 'campus'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # NULL for a post by a student who belongs to no chapter (c71). Present on
    # every org post, and also on a campus post made from inside a chapter, where
    # it stays as provenance so the post still shows in that chapter's own feed.
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id")
    )
    # Always set, including for org posts. The campus feed reads this instead of
    # joining through chapters, so a chapter-less post is reachable at all; it also
    # pins the post to the campus it was made on, which deriving campus from the
    # author would not do once that author transfers campuses.
    campus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_urls: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # 'org' (chapter-public - any non-removed member), 'campus' (visible campus-wide),
    # or 'org_actives' (chapter-scoped like 'org', but only a viewer whose OWN
    # membership.status == 'active' sees it - board c102). Author-chosen at compose
    # time, defaults to 'org' so a client that omits it never accidentally broadcasts
    # (board Decisions log, Aug 14).
    audience: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'org'")
    )
    post_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'text'")
    )
    duration_sec: Mapped[int | None] = mapped_column(Integer)  # video posts only
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only by moderator removal (routers/moderation.py remove_content), never by the
    # author/president self-delete in routers/feed.py — so NULL vs set is what tells a
    # self-delete apart from a moderator's removal even though both share deleted_at.
    removed_reason: Mapped[str | None] = mapped_column(Text)


class PostLike(Base):
    __tablename__ = "post_likes"

    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posts.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PostComment(Base):
    __tablename__ = "post_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posts.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # See Post.removed_reason: set only by moderator removal, never by ordinary delete.
    removed_reason: Mapped[str | None] = mapped_column(Text)
