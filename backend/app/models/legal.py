"""Versioned legal policies and append-only account acceptance records (c438)."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LegalPolicy(Base):
    __tablename__ = "legal_policies"
    __table_args__ = (
        UniqueConstraint("policy_key", "version", name="uq_legal_policy_key_version"),
        Index("uq_legal_policy_current_key", "policy_key", unique=True, postgresql_where=text("is_current")),
        CheckConstraint("policy_key IN ('terms', 'privacy')", name="ck_legal_policy_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    policy_key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class LegalAcceptance(Base):
    __tablename__ = "legal_acceptances"
    __table_args__ = (
        UniqueConstraint("user_id", "policy_id", name="uq_legal_acceptance_user_policy"),
        Index("ix_legal_acceptances_user", "user_id"),
        CheckConstraint("age_declaration IN (17, 18)", name="ck_legal_acceptance_age"),
        CheckConstraint("age_declaration >= 18 OR guardian_permission_confirmed", name="ck_legal_acceptance_guardian"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("legal_policies.id"), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    age_declaration: Mapped[int] = mapped_column(Integer, nullable=False)
    guardian_permission_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'mobile'"))


class OrganizationAuthorityAcceptance(Base):
    __tablename__ = "organization_authority_acceptances"
    __table_args__ = (
        CheckConstraint("purpose IN ('organization_create', 'payment_setup')", name="ck_authority_purpose"),
        Index("ix_org_authority_user_chapter", "user_id", "chapter_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False)
    membership_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("memberships.id"), nullable=False)
    role_term_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("role_terms.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_account_id: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
