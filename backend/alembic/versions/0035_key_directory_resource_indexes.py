"""Index retained key quotas and bounded recipient-device lookups (c347)."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep retained-row checks indexed without removing available-pool indexes."""
    op.create_index(
        "idx_devices_user_revoked_created", "devices",
        ["user_id", "revoked_at", "created_at", "id"],
    )
    op.create_index(
        "idx_signed_prekeys_device_created", "signed_prekeys",
        ["device_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("idx_otk_device_retained", "one_time_prekeys", ["device_id"])
    op.create_index(
        "idx_kyber_device_kind_created", "kyber_prekeys",
        ["device_id", "is_last_resort", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    """Remove only these query indexes; key material and older partial indexes survive."""
    op.drop_index("idx_kyber_device_kind_created", table_name="kyber_prekeys")
    op.drop_index("idx_otk_device_retained", table_name="one_time_prekeys")
    op.drop_index("idx_signed_prekeys_device_created", table_name="signed_prekeys")
    op.drop_index("idx_devices_user_revoked_created", table_name="devices")
