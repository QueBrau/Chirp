"""Add workplace/job location fields for alumni directory + job board."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alumni_profiles ADD COLUMN location TEXT")
    op.execute("ALTER TABLE job_posts ADD COLUMN location TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE job_posts DROP COLUMN IF EXISTS location")
    op.execute("ALTER TABLE alumni_profiles DROP COLUMN IF EXISTS location")
