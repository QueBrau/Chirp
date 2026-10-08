"""Persist retry-safe account-deletion fulfillment steps (c436)."""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_fulfillment_steps",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("request_id", sa.UUID(), sa.ForeignKey("account_data_requests.id"), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("provider_ref", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "step_key", name="uq_account_fulfillment_step"),
        sa.CheckConstraint("status IN ('pending','running','succeeded','failed','manual_review')", name="ck_account_fulfillment_step_status"),
    )
    op.create_index("ix_account_fulfillment_steps_request", "account_fulfillment_steps", ["request_id", "step_key"])


def downgrade() -> None:
    op.drop_index("ix_account_fulfillment_steps_request", table_name="account_fulfillment_steps")
    op.drop_table("account_fulfillment_steps")
