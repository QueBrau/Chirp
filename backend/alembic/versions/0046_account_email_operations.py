"""Admit campus verification email operations to the account journal (c436)."""
from alembic import op


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_account_external_operation_kind", "account_external_operations", type_="check",
    )
    op.create_check_constraint(
        "ck_account_external_operation_kind", "account_external_operations",
        "kind IN ('media_finalize_post', 'media_finalize_avatar', 'campus_verification_email')",
    )


def downgrade() -> None:
    # Existing email registrations are safety evidence. If any remain, PostgreSQL
    # rejects the narrower check and rolls back this migration; never delete them
    # merely to make a downgrade succeed.
    op.drop_constraint(
        "ck_account_external_operation_kind", "account_external_operations", type_="check",
    )
    op.create_check_constraint(
        "ck_account_external_operation_kind", "account_external_operations",
        "kind IN ('media_finalize_post', 'media_finalize_avatar')",
    )
