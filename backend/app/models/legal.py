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
