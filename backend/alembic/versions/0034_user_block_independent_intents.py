"""Preserve independent anonymous block intent through named operations (c342)."""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Previous named upgrades destroyed provenance. Every existing row already hid
    # Chirps, so retain that protection without guessing which rows began anonymously.
    # The default also protects legacy writers between migration and deployment.
    # New named-only writes explicitly provide SQL NULL and never affect Chirps.
    op.add_column(
        "user_blocks",
        sa.Column(
            "anonymous_created_at", sa.DateTime(timezone=True), nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.execute("UPDATE user_blocks SET anonymous_created_at = created_at")
    op.create_check_constraint(
        "ck_user_blocks_anonymous_intent", "user_blocks",
        "source = 'named' OR anonymous_created_at IS NOT NULL",
    )


def downgrade() -> None:
    # Keep every pair and its named/by_chirp source. Older code enforces contact
    # from either source and hides Chirps for both, so no surviving protection is
    # lost by collapsing the marker. Independent intent and the privacy fix ARE
    # lost: a downgrade is not safe evidence that c342 remains closed.
    op.drop_constraint("ck_user_blocks_anonymous_intent", "user_blocks", type_="check")
    op.drop_column("user_blocks", "anonymous_created_at")
