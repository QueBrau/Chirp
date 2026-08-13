"""Recreate idx_messages_convo_time as a compound (conversation_id, created_at, id) index.

SECURITY-REVIEW.md finding 10: message pagination already cursors on
(created_at, id) to avoid dropping tied-timestamp rows at a page boundary, but
the index backing that query was created_at-only (and missing conversation_id
entirely), so the compound ORDER BY couldn't be served by an index scan. This
drops the old index and recreates it matching the query shape exactly.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_messages_convo_time")
    op.execute(
        "CREATE INDEX idx_messages_convo_time ON messages "
        "(conversation_id, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_messages_convo_time")
    op.execute(
        "CREATE INDEX idx_messages_convo_time ON messages (conversation_id, created_at DESC)"
    )
