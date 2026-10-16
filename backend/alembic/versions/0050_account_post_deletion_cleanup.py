"""Persist account-owned deferred cleanup after identity tombstoning."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_post_deletion_cleanup",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("scope_sha256", sa.Text(), nullable=False),
        sa.Column("last_pass_digest", sa.Text(), nullable=False),
        sa.Column("remaining_count", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("owner_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("category IN ('temporary_media', 'identifiable_analytics')", name="ck_account_post_deletion_cleanup_category"),
        sa.CheckConstraint("state IN ('pending', 'running', 'uncertain', 'complete', 'manual_review')", name="ck_account_post_deletion_cleanup_state"),
        sa.CheckConstraint("attempt_count >= 0 AND attempt_count <= 1000000", name="ck_account_post_deletion_cleanup_attempts"),
        sa.CheckConstraint("scope_sha256 ~ '^[0-9a-f]{64}$'", name="ck_account_post_deletion_cleanup_scope_digest"),
        sa.CheckConstraint("last_pass_digest ~ '^[0-9a-f]{64}$'", name="ck_account_post_deletion_cleanup_pass_digest"),
        sa.CheckConstraint("remaining_count IS NULL OR (remaining_count >= 0 AND remaining_count <= 10000)", name="ck_account_post_deletion_cleanup_remaining_count"),
        sa.ForeignKeyConstraint(["request_id"], ["account_data_requests.id"], name="fk_account_post_deletion_cleanup_request"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_account_post_deletion_cleanup_user"),
        sa.PrimaryKeyConstraint("id", name="pk_account_post_deletion_cleanup"),
        sa.UniqueConstraint("request_id", "category", name="uq_account_post_deletion_cleanup_request_category"),
    )
    op.create_index("ix_account_post_deletion_cleanup_due", "account_post_deletion_cleanup", ["state", "next_attempt_at"])
    op.create_index("ix_account_post_deletion_cleanup_user", "account_post_deletion_cleanup", ["user_id", "state"])


def downgrade() -> None:
    op.execute("LOCK TABLE account_post_deletion_cleanup IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM account_post_deletion_cleanup) THEN
                RAISE EXCEPTION 'post_deletion_cleanup_evidence_not_empty';
            END IF;
        END $$;
    """)
    op.drop_index("ix_account_post_deletion_cleanup_user", table_name="account_post_deletion_cleanup")
    op.drop_index("ix_account_post_deletion_cleanup_due", table_name="account_post_deletion_cleanup")
    op.drop_table("account_post_deletion_cleanup")
