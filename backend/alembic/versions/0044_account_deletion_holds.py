"""Add durable account-deletion write holds (c436 primitive)."""
from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_deletion_holds",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.UUID(), sa.ForeignKey("account_data_requests.id"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("reason", sa.Text(), server_default=sa.text("'account_deletion'"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_account_deletion_hold_request"),
        sa.CheckConstraint("state = 'active'", name="ck_account_deletion_hold_state"),
        sa.CheckConstraint("reason = 'account_deletion'", name="ck_account_deletion_hold_reason"),
    )
    op.create_index(
        "uq_account_deletion_hold_active_user",
        "account_deletion_holds",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_account_deletion_hold_active_user", table_name="account_deletion_holds")
    op.drop_table("account_deletion_holds")
