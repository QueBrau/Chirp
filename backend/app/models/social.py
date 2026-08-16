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
        CheckConstraint(
            "audience IN ('org', 'campus')",
            name="ck_posts_audience",
        ),
        CheckConstraint(
            "post_type IN ('text', 'photo', 'video')",
            name="ck_posts_post_type",
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
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_urls: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # 'org' (private to the chapter) or 'campus' (visible campus-wide); author-chosen
    # at compose time, defaults to 'org' so a client that omits it never accidentally
    # broadcasts (board Decisions log, Aug 14).
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
