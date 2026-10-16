"""Journal immutable logical payment effects and provider attempts (c436)."""
from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
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
        "'campus_verification_email', 'payment_request')",
    )
    op.create_table(
        "account_payment_effects",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("effect_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("connected_account_id", sa.Text()),
        # Deliberately not an FK: registration may happen while the caller holds
        # the dues reservation's row lock, and a key-share FK check can deadlock.
        sa.Column("reservation_id", sa.UUID()),
        sa.Column("provider_object_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "effect_key", name="uq_account_payment_effect_key"),
        sa.CheckConstraint(
            "kind IN ('connect_account_create', 'connect_account_link', "
            "'customer_create', 'customer_session_create', "
            "'payment_intent_create', 'payment_intent_cancel')",
            name="ck_account_payment_effect_kind",
        ),
        sa.CheckConstraint("effect_key ~ '^[0-9a-f]{64}$'", name="ck_account_payment_effect_key_sha256"),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_account_payment_effect_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_account_payment_effect_idempotency_key",
        ),
        sa.CheckConstraint(
            "connected_account_id IS NULL OR connected_account_id ~ '^acct_[A-Za-z0-9]{1,240}$'",
            name="ck_account_payment_effect_connected_account",
        ),
        sa.CheckConstraint(
            "(kind = 'connect_account_create' AND connected_account_id IS NULL AND reservation_id IS NULL) OR "
            "(kind = 'connect_account_link' AND connected_account_id IS NOT NULL AND reservation_id IS NULL) OR "
            "(kind IN ('customer_create', 'customer_session_create') AND connected_account_id IS NOT NULL AND reservation_id IS NULL) OR "
            "(kind IN ('payment_intent_create', 'payment_intent_cancel') AND connected_account_id IS NOT NULL AND reservation_id IS NOT NULL)",
            name="ck_account_payment_effect_scope_shape",
        ),
        sa.CheckConstraint(
            "provider_object_id IS NULL OR provider_object_id ~ '^(acct|cus|pi)_[A-Za-z0-9]{1,240}$'",
            name="ck_account_payment_effect_provider_id",
        ),
    )
    op.create_index(
        "ix_account_payment_effects_kind_created",
        "account_payment_effects",
        ["kind", "created_at"],
    )
    op.create_table(
        "account_payment_attempts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("effect_id", sa.UUID(), nullable=False),
        sa.Column("parent_operation_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("owner_token", sa.UUID(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["effect_id"], ["account_payment_effects.id"]),
        sa.ForeignKeyConstraint(["parent_operation_id"], ["account_external_operations.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('running', 'uncertain', 'finished', 'reconciled')",
            name="ck_account_payment_attempt_state",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('uncertain', 'finished', 'reconciled') AND completed_at IS NOT NULL)",
            name="ck_account_payment_attempt_completion_time",
        ),
    )
    op.create_index(
        "ix_account_payment_attempts_user_state",
        "account_payment_attempts",
        ["actor_user_id", "state"],
    )
    op.create_index(
        "ix_account_payment_attempts_effect_state",
        "account_payment_attempts",
        ["effect_id", "state"],
    )
    op.create_index(
        "ix_account_payment_attempts_parent",
        "account_payment_attempts",
        ["parent_operation_id"],
    )


def downgrade() -> None:
    # Freeze writers before inspecting evidence: checking emptiness and then
    # dropping without this lock could erase a concurrently committed attempt.
    # Match registration's parent -> effect -> attempt order.
    op.execute(
        "LOCK TABLE account_external_operations, account_payment_effects, "
        "account_payment_attempts IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM account_payment_effects) THEN
                RAISE EXCEPTION 'account_payment_operations_not_empty';
            END IF;
            IF EXISTS (
                SELECT 1 FROM account_external_operations WHERE kind = 'payment_request'
            ) THEN
                RAISE EXCEPTION 'payment_request_operations_not_empty';
            END IF;
        END $$;
    """)
    op.drop_index("ix_account_payment_attempts_parent", table_name="account_payment_attempts")
    op.drop_index("ix_account_payment_attempts_effect_state", table_name="account_payment_attempts")
    op.drop_index("ix_account_payment_attempts_user_state", table_name="account_payment_attempts")
    op.drop_table("account_payment_attempts")
    op.drop_index("ix_account_payment_effects_kind_created", table_name="account_payment_effects")
    op.drop_table("account_payment_effects")
    op.drop_constraint(
        "ck_account_external_operation_kind",
        "account_external_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_account_external_operation_kind",
        "account_external_operations",
        "kind IN ('media_finalize_post', 'media_finalize_avatar', 'campus_verification_email')",
    )
