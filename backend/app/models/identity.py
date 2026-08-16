"""Identity & org models: users, campuses, chapters, memberships, invites (SPEC §3)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('greek', 'non_greek', 'alumni')",
            name="ck_users_account_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    firebase_uid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str] = mapped_column(Text, nullable=False)
    campus_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id")
    )
    # Placeholder members for historical lineage.
    is_ghost: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Platform-admin gate for chapter creation (SECURITY-REVIEW.md, board card c28).
    # No API sets this — flipped directly in the DB.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Account suspension (board card c76): NULL == not suspended. Checked on every
    # request in middleware/auth.get_current_user, so a suspended account is rejected
    # everywhere rather than per-route. Cleared back to NULL on unsuspend — the durable
    # history of who/when/why lives in moderation_actions, not here.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspension_reason: Mapped[str | None] = mapped_column(Text)
    suspended_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    # .edu verification (board c86/c88). NULL == never verified. This is the ONLY thing
    # that may open the campus feed and the Yak board — deliberately NOT campus_id.
    #
    # WHY THE DISTINCTION IS LOAD-BEARING: campus_id is reachable without any email at
    # all. c96 made redeeming a chapter invite write it, and per c105 an invite code is
    # an unlimited-use bearer token. So a gate that asks "does this user have a campus"
    # is satisfied by a forwarded code, which is precisely what Jose's c88 ruling says
    # must not grant campus-wide access. A gate that asks "when did this user last prove
    # an .edu address" is not. Read app.core.campus_access, never this column directly.
    campus_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Campus(Base):
    __tablename__ = "campuses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # Addresses that prove attendance here, lowercase and without the '@' (c86).
    # Empty or NULL means this campus cannot be verified at all, which fails CLOSED:
    # no domain configured means no code is ever sent, rather than any address passing.
    # Subdomain policy is deliberately EXACT MATCH — "uncg.edu" does not admit
    # "students.uncg.edu". A campus that needs the subdomain lists it explicitly, so
    # widening the rule is always a visible data change rather than a silent one.
    email_domains: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    campus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id"), nullable=False
    )
    org_name: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "Sigma Chi"
    chapter_name: Mapped[str | None] = mapped_column(Text)  # e.g. "Epsilon Mu"
    # Stripe Connect connected account.
    stripe_account_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('president','vice_president','treasurer','secretary',"
            "'historian','member','pledge','alumni')",
            name="ck_memberships_role",
        ),
        CheckConstraint(
            "status IN ('active','inactive','removed')",
            name="ck_memberships_status",
        ),
        UniqueConstraint("user_id", "chapter_id", name="uq_memberships_user_chapter"),
        Index(
            "idx_memberships_chapter",
            "chapter_id",
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "idx_memberships_user",
            "user_id",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    pledge_class: Mapped[str | None] = mapped_column(Text)  # e.g. "Fall 2024"
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ChapterInvite(Base):
    """A redeemable chapter invite. c105 made it a bounded credential, not a bearer
    token: every code expires, every code has a redemption budget, and any of them
    can be killed outright."""

    __tablename__ = "chapter_invites"
    __table_args__ = (
        CheckConstraint(
            "max_uses >= 1 AND max_uses <= 200",
            name="ck_chapter_invites_max_uses_range",
        ),
        CheckConstraint("uses >= 0", name="ck_chapter_invites_uses_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    # Deep-link invite code.
    code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'member'")
    )
    # NOT NULL since 0016 (c105): a never-expiring code is not representable here.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Redemption budget. There is deliberately no "unlimited" value — see 0016.
    max_uses: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("25")
    )
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class CampusVerification(Base):
    """One .edu verification attempt: a code mailed to an address, and its outcome (c86).

    A row is created when a code is sent and consumed when the right code comes back.
    Rows are kept after consumption rather than deleted — users.campus_verified_at says
    THAT someone is verified, and this table is the durable record of which address
    proved it and when, which is what an abuse report or a yearly re-check needs.

    ON STORING THE CODE HASHED, and on being honest about what that buys: a six-digit
    code has only a million values, so hashing does NOT make a stolen database safe on
    its own — an attacker who can read this table can enumerate a row's code offline.
    Salting with the row id defeats a single precomputed table across all rows, and the
    protections that actually matter are the ones on the live path: a short expiry, a
    hard attempt cap, and single use. Those are enforced in app.services.campus_verification.
    What hashing genuinely prevents is the ordinary case - a code being read straight
    out of a backup, a log, or over the shoulder of anyone with a psql prompt.
    """

    __tablename__ = "campus_verifications"
    __table_args__ = (
        # The pending-code lookup: newest unconsumed row for a user.
        Index(
            "ix_campus_verifications_user_pending",
            "user_id",
            "sent_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # The campus this code would prove, resolved from the address's domain at SEND time
    # rather than taken from the client. A request body that names both the address and
    # the campus lets a caller pair someone else's domain with the campus they want.
    campus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campuses.id"), nullable=False
    )
    edu_email: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256(f"{id}:{code}"). The plaintext code exists only in the sent message.
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL until the correct code is submitted. Set once and never cleared, so a row can
    # never be redeemed twice even if the same code is replayed.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Wrong-code submissions against THIS row. Capped, so a live brute force runs out of
    # tries long before it runs out of the million possible codes.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
