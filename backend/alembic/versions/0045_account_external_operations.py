"""Journal admitted account-scoped external operations (c436)."""
from alembic import op
import sqlalchemy as sa


revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_external_operations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("effect_key", sa.Text(), nullable=False),
        sa.Column("owner_token", sa.UUID(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id", "kind", "effect_key",
            name="uq_account_external_operation_effect",
        ),
        sa.CheckConstraint(
            "kind IN ('media_finalize_post', 'media_finalize_avatar')",
            name="ck_account_external_operation_kind",
        ),
        sa.CheckConstraint(
            "effect_key ~ '^[0-9a-f]{64}$'",
            name="ck_account_external_operation_effect_key",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'finished', 'uncertain')",
            name="ck_account_external_operation_state",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('finished', 'uncertain') AND completed_at IS NOT NULL)",
            name="ck_account_external_operation_completion_time",
        ),
    )
    op.create_index(
        "ix_account_external_operations_actor",
        "account_external_operations",
        ["actor_user_id"],
    )
    op.create_table(
        "account_external_operation_scopes",
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["operation_id"], ["account_external_operations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("operation_id", "user_id"),
    )
    op.create_index(
        "ix_account_external_operation_scopes_user",
        "account_external_operation_scopes",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_external_operation_scopes_user",
        table_name="account_external_operation_scopes",
    )
    op.drop_table("account_external_operation_scopes")
    op.drop_index(
        "ix_account_external_operations_actor",
        table_name="account_external_operations",
    )
    op.drop_table("account_external_operations")
