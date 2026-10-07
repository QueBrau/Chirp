"""Versioned Terms/Privacy acceptance with age and guardian attestation (c438)."""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legal_policies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("policy_key", sa.Text(), nullable=False), sa.Column("version", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("policy_key", "version", name="uq_legal_policy_key_version"),
        sa.CheckConstraint("policy_key IN ('terms', 'privacy')", name="ck_legal_policy_key"),
    )
    op.create_index("uq_legal_policy_current_key", "legal_policies", ["policy_key"], unique=True, postgresql_where=sa.text("is_current"))
    op.create_table(
        "legal_acceptances",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("policy_id", sa.UUID(), sa.ForeignKey("legal_policies.id"), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("age_declaration", sa.Integer(), nullable=False),
        sa.Column("guardian_permission_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'mobile'"), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("user_id", "policy_id", name="uq_legal_acceptance_user_policy"),
        sa.CheckConstraint("age_declaration IN (17, 18)", name="ck_legal_acceptance_age"),
        sa.CheckConstraint("age_declaration >= 18 OR guardian_permission_confirmed", name="ck_legal_acceptance_guardian"),
    )
    op.create_index("ix_legal_acceptances_user", "legal_acceptances", ["user_id"])
    op.execute("INSERT INTO legal_policies (policy_key, version) VALUES ('terms', '2026-10-06'), ('privacy', '2026-10-06')")


def downgrade() -> None:
    op.drop_index("uq_legal_policy_current_key", table_name="legal_policies")
    op.drop_index("ix_legal_acceptances_user", table_name="legal_acceptances")
    op.drop_table("legal_acceptances")
    op.drop_table("legal_policies")
