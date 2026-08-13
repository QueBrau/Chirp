"""Add users.is_platform_admin (board card c28, SECURITY-REVIEW.md finding 1: gate chapter creation).

No API sets this column — platform admins are flipped directly in the DB.
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add is_platform_admin, default false, backfilling existing rows."""
    op.execute(
        "ALTER TABLE users ADD COLUMN is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    """Drop the is_platform_admin column."""
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_platform_admin")
