"""Bounded delivery admission evidence and outbox claim fencing (c436)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("delivery_outbox", sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_table(
        "account_delivery_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("slot", sa.SmallInteger(), nullable=False),
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "slot", name="uq_account_delivery_attempt_slot"),
        sa.UniqueConstraint("outbox_id", "user_id", name="uq_account_delivery_attempt_recipient"),
        sa.CheckConstraint("slot >= 0 AND slot < 32", name="ck_account_delivery_attempt_slot"),
        sa.CheckConstraint("state IN ('running', 'uncertain')", name="ck_account_delivery_attempt_state"),
    )
    op.create_index("ix_account_delivery_attempts_user_id", "account_delivery_attempts", ["user_id"])


def downgrade() -> None:
    # Both an unresolved effect and an outstanding claim fence are evidence of
    # work that the old dispatcher cannot safely own. Never erase either silently.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM account_delivery_attempts)
               OR EXISTS (SELECT 1 FROM delivery_outbox WHERE claim_token IS NOT NULL) THEN
                RAISE EXCEPTION 'account_delivery_attempts_not_drained';
            END IF;
        END $$;
    """)
    op.drop_index("ix_account_delivery_attempts_user_id", table_name="account_delivery_attempts")
    op.drop_table("account_delivery_attempts")
    op.drop_column("delivery_outbox", "claim_token")
