"""E2EE key directory models: devices, signed prekeys, one-time prekeys (SPEC §3)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    device_label: Mapped[str | None] = mapped_column(Text)
    # libsignal registration id.
    registration_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Public identity key.
    identity_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignedPrekey(Base):
    __tablename__ = "signed_prekeys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False
    )
    key_id: Mapped[int] = mapped_column(Integer, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OneTimePrekey(Base):
    __tablename__ = "one_time_prekeys"
    __table_args__ = (
        Index(
            "idx_otk_available",
            "device_id",
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False
    )
    key_id: Mapped[int] = mapped_column(Integer, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Server hands out once, marks consumed.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
