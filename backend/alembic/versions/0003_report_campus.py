"""Add content_reports.campus_id so moderation reads can be scoped to a campus (SECURITY-REVIEW #1)."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable campus_id FK to content_reports; resolved server-side on report creation."""
    op.execute(
        "ALTER TABLE content_reports ADD COLUMN campus_id UUID REFERENCES campuses(id)"
    )
    op.execute(
        "CREATE INDEX idx_content_reports_campus ON content_reports(campus_id)"
    )


def downgrade() -> None:
    """Drop the campus_id column and its index."""
    op.execute("DROP INDEX IF EXISTS idx_content_reports_campus")
    op.execute("ALTER TABLE content_reports DROP COLUMN IF EXISTS campus_id")
