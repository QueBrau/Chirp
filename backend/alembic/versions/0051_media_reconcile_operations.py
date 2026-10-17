"""Admit permanent media reconciliation deletes to the account drain registry."""
from alembic import op


revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_account_external_operation_kind",
        "account_external_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_account_external_operation_kind",
        "account_external_operations",
        "kind IN ('media_finalize_post', 'media_finalize_avatar', "
        "'campus_verification_email', 'payment_request', 'media_reconcile_post')",
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE account_external_operations IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM account_external_operations
                WHERE kind = 'media_reconcile_post'
            ) THEN
                RAISE EXCEPTION 'media_reconcile_operations_not_empty';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        "ck_account_external_operation_kind",
        "account_external_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_account_external_operation_kind",
        "account_external_operations",
        "kind IN ('media_finalize_post', 'media_finalize_avatar', "
        "'campus_verification_email', 'payment_request')",
    )
