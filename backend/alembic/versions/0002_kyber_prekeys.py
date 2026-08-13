"""Kyber (PQXDH) prekey directory: signed last-resort + optional one-time kyber prekeys."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add kyber_prekeys table (SPEC §3) alongside the existing EC prekey tables."""
    op.execute(
        """
        CREATE TABLE kyber_prekeys (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id       UUID NOT NULL REFERENCES devices(id),
            key_id          INTEGER NOT NULL,
            public_key      BYTEA NOT NULL,
            signature       BYTEA NOT NULL,        -- signed by the device identity key
            is_last_resort  BOOLEAN NOT NULL DEFAULT FALSE,
            consumed_at     TIMESTAMPTZ,            -- one-time kybers only; last-resort never consumed
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_kyber_otk_available ON kyber_prekeys(device_id) "
        "WHERE consumed_at IS NULL AND NOT is_last_resort"
    )


def downgrade() -> None:
    """Drop kyber_prekeys."""
    op.execute("DROP TABLE IF EXISTS kyber_prekeys")
