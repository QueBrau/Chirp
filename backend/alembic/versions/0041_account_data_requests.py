"""Add authenticated account export/deletion requests and immutable export artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_data_requests",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'received'"), nullable=False),
        sa.Column("open_key", sa.Text()),
        sa.Column("scope", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("excluded", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("retention_reasons", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("failure_code", sa.Text()),
        sa.Column("provider_steps", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('export', 'deletion')", name="ck_account_data_request_kind"),
        sa.CheckConstraint(
            "status IN ('received','verifying','processing','ready','completed','partially_completed','blocked','failed','canceled')",
            name="ck_account_data_request_status",
        ),
    )
    op.create_index("ix_account_data_requests_user_created", "account_data_requests", ["user_id", "created_at"])
    op.create_index(
        "uq_account_data_request_open",
        "account_data_requests",
        ["user_id", "kind", "open_key"],
        unique=True,
        postgresql_where=sa.text("open_key IS NOT NULL"),
    )
    op.create_table(
        "account_data_artifacts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("request_id", sa.UUID(), sa.ForeignKey("account_data_requests.id"), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("content_sha256"),
    )


def downgrade() -> None:
    op.drop_table("account_data_artifacts")
    op.drop_index("uq_account_data_request_open", table_name="account_data_requests")
    op.drop_index("ix_account_data_requests_user_created", table_name="account_data_requests")
    op.drop_table("account_data_requests")
