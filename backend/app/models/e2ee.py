"""E2EE key directory models: devices, signed/one-time prekeys, Kyber (PQXDH) prekeys (SPEC §3).

The v2 (vodozemac/Olm) columns on Device and OneTimePrekey are board c444 / migration 0042.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Device(Base):
    """A registered device. `crypto_suite IS NULL` is a LEGACY (Signal-shaped, v1) row.

    A non-NULL suite is an E2EE v2 device (board c444, migration 0042): `identity_key`
    then holds the Curve25519 identity and the v2-only columns below are all populated.
    The CHECK constraints in migration 0042 keep the two shapes exclusive; they are
    mirrored here so the metadata describes the real table.
    """

    __tablename__ = "devices"
    __table_args__ = (
        Index("idx_devices_user_revoked_created", "user_id", "revoked_at", "created_at", "id"),
        CheckConstraint(
            "crypto_suite IS NULL OR ("
            "length(crypto_suite) BETWEEN 1 AND 64"
            " AND identity_ed25519 IS NOT NULL AND length(identity_ed25519) = 32"
            " AND length(identity_key) = 32"
            " AND generation IS NOT NULL AND generation BETWEEN 1 AND 4294967295"
            " AND binding_signature IS NOT NULL AND length(binding_signature) = 64)",
            name="ck_devices_v2_shape",
        ),
        CheckConstraint(
            "crypto_suite IS NOT NULL OR ("
            "identity_ed25519 IS NULL AND generation IS NULL"
            " AND binding_signature IS NULL AND approved_at IS NULL"
            " AND approved_by_device_id IS NULL AND approval_signature IS NULL)",
            name="ck_devices_legacy_has_no_v2_fields",
        ),
        CheckConstraint(
            "crypto_suite IS NOT NULL OR registration_id IS NOT NULL",
            name="ck_devices_registration_id",
        ),
        CheckConstraint(
            "(approved_by_device_id IS NULL AND approval_signature IS NULL) OR ("
            "approved_by_device_id IS NOT NULL"
            " AND approval_signature IS NOT NULL AND length(approval_signature) = 64"
            " AND approved_at IS NOT NULL AND approved_by_device_id <> id)",
            name="ck_devices_approval_shape",
        ),
        Index(
            "uq_devices_v2_user_generation", "user_id", "generation", unique=True,
            postgresql_where=text("crypto_suite IS NOT NULL"),
        ),
        Index(
            "uq_devices_v2_identity_curve25519", "identity_key", unique=True,
            postgresql_where=text("crypto_suite IS NOT NULL"),
        ),
        Index(
            "uq_devices_v2_identity_ed25519", "identity_ed25519", unique=True,
            postgresql_where=text("crypto_suite IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    device_label: Mapped[str | None] = mapped_column(Text)
    # libsignal registration id. NULL only for v2 devices (Olm has no registration id);
    # ck_devices_registration_id keeps it mandatory for legacy rows.
    registration_id: Mapped[int | None] = mapped_column(Integer)
    # Public identity key. For a v2 device this is the Curve25519 identity (32 bytes).
    identity_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- E2EE v2 (board c444, migration 0042); all NULL on a legacy row ----
    # Suite identifier, e.g. "vodozemac-olm-v1". NULL = legacy and refused by v2.
    crypto_suite: Mapped[str | None] = mapped_column(Text)
    identity_ed25519: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Client-chosen u32, strictly increasing per account across ALL of its device rows
    # (including revoked and pending). BIGINT because a u32 does not fit INTEGER.
    generation: Mapped[int | None] = mapped_column(BigInteger)
    binding_signature: Mapped[bytes | None] = mapped_column(LargeBinary)
    # The explicit approved marker. Pending = NULL. Root (self-approved first device) =
    # set with approved_by_device_id NULL. Without this column, pending and root would
    # both read as "approved_by IS NULL".
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL on a root device: the account's first device has no approver.
    approved_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id")
    )
    approval_signature: Mapped[bytes | None] = mapped_column(LargeBinary)


class SignedPrekey(Base):
    __tablename__ = "signed_prekeys"
    __table_args__ = (
        Index(
            "idx_signed_prekeys_device_created", "device_id",
            text("created_at DESC"), text("id DESC"),
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
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OneTimePrekey(Base):
    """A one-time prekey, or (kind = 'fallback', board c444) a v2 device's fallback key.

    v1 rows have kind = 'one_time' and a NULL signature. A v2 row always has a signature
    (an Ed25519 signature by the device's identity key, verified at upload). A fallback
    row is never consumed: claims hand it out only when the one-time pool is empty, and
    replacing it deletes the old row, so there is at most one per device.
    """

    __tablename__ = "one_time_prekeys"
    __table_args__ = (
        Index("idx_otk_device_retained", "device_id"),
        Index(
            "idx_otk_available",
            "device_id",
            postgresql_where=text("consumed_at IS NULL"),
        ),
        CheckConstraint("kind IN ('one_time', 'fallback')", name="ck_otk_kind"),
        CheckConstraint(
            "signature IS NULL OR ("
            "length(signature) = 64 AND length(public_key) = 32 AND key_id >= 0)",
            name="ck_otk_v2_shape",
        ),
        CheckConstraint(
            "kind = 'one_time' OR signature IS NOT NULL", name="ck_otk_fallback_signed"
        ),
        # A key id is never reused within a device (vodozemac ids are u64 and monotonic).
        Index(
            "uq_otk_v2_device_key_id", "device_id", "key_id", unique=True,
            postgresql_where=text("signature IS NOT NULL"),
        ),
        Index(
            "uq_otk_v2_one_fallback_per_device", "device_id", unique=True,
            postgresql_where=text("kind = 'fallback'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False
    )
    # BIGINT: vodozemac key ids are u64 (the v2 API keeps them below 2**63).
    key_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Server hands out once, marks consumed.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'one_time'")
    )
    signature: Mapped[bytes | None] = mapped_column(LargeBinary)


class KyberPrekey(Base):
    """PQXDH Kyber prekey: one signed last-resort per device (never consumed) plus an
    optional pool of one-time Kyber prekeys (consumed like `OneTimePrekey`)."""

    __tablename__ = "kyber_prekeys"
    __table_args__ = (
        Index(
            "idx_kyber_device_kind_created", "device_id", "is_last_resort",
            text("created_at DESC"), text("id DESC"),
        ),
        Index(
            "idx_kyber_otk_available",
            "device_id",
            postgresql_where=text("consumed_at IS NULL AND NOT is_last_resort"),
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
    # Signed by the device identity key (client-side, not verified server-side —
    # same trust boundary as SignedPrekey.signature).
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_last_resort: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # One-time kybers: server hands out once, marks consumed. Last-resort rows are never consumed.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
