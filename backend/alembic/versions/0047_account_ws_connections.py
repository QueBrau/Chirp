"""Bounded active WebSocket registrations for deletion drain (c436)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_ws_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("slot", sa.SmallInteger(), nullable=False),
        sa.Column("owner_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "slot", name="uq_account_ws_connection_slot"),
        sa.CheckConstraint("slot >= 0 AND slot < 32", name="ck_account_ws_connection_slot"),
        sa.CheckConstraint("state IN ('running', 'uncertain')", name="ck_account_ws_connection_state"),
    )
    op.create_index("ix_account_ws_connections_user_id", "account_ws_connections", ["user_id"])


def downgrade() -> None:
    # An abandoned or uncertain connection is evidence that deletion must wait.
    # Downgrade must not erase that evidence to make the old schema installable.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM account_ws_connections) THEN
                RAISE EXCEPTION 'account_ws_connections_not_drained';
            END IF;
        END $$;
    """)
    op.drop_index("ix_account_ws_connections_user_id", table_name="account_ws_connections")
    op.drop_table("account_ws_connections")
