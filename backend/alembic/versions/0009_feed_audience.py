"""Feed audience: posts.audience (org|campus), post_type, duration_sec (board card c34).

Product decision (Aug 12/14, board Decisions log): a post's audience is chosen by
its author at compose time. 'org' means private to the chapter (posts never
surface on the public/campus feed); 'campus' means visible to everyone at that
campus. "For You" is a ranking over campus posts, not a third stored value.

The column defaults to 'org' and existing rows are NEVER backfilled to 'campus':
every post that existed before this migration was implicitly a chapter-only post,
and flipping the default the other way would silently broadcast private chapter
content to the whole campus.

post_type/duration_sec round out the FYP media contract (photo/video cards);
duration_sec is video-only and stays NULL for text/photo posts.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE posts ADD COLUMN audience TEXT NOT NULL DEFAULT 'org' "
        "CHECK (audience IN ('org', 'campus'))"
    )
    op.execute(
        "ALTER TABLE posts ADD COLUMN post_type TEXT NOT NULL DEFAULT 'text' "
        "CHECK (post_type IN ('text', 'photo', 'video'))"
    )
    op.execute("ALTER TABLE posts ADD COLUMN duration_sec INTEGER")
    op.execute(
        "CREATE INDEX idx_posts_audience_time ON posts (audience, created_at DESC) "
        "WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_posts_audience_time")
    op.execute("ALTER TABLE posts DROP COLUMN IF EXISTS duration_sec")
    op.execute("ALTER TABLE posts DROP COLUMN IF EXISTS post_type")
    op.execute("ALTER TABLE posts DROP COLUMN IF EXISTS audience")
