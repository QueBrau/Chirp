"""Events (board card c33): chapter events (Partiful-style) and RSVPs.

Any active member may host an event, so events has no e-board-only column the
way meetings.created_by is secretary/president-gated at the router level.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add events and event_rsvps tables."""
    op.execute(
        """
        CREATE TABLE events (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            title       TEXT NOT NULL,
            cover_url   TEXT NOT NULL,
            date_label  TEXT NOT NULL,      -- plain human-readable string, no datetime parsing
            location    TEXT NOT NULL,
            host_id     UUID NOT NULL REFERENCES users(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_events_chapter_time ON events(chapter_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE event_rsvps (
            event_id    UUID NOT NULL REFERENCES events(id),
            user_id     UUID NOT NULL REFERENCES users(id),
            status      TEXT NOT NULL CHECK (status IN ('going','maybe','cant')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (event_id, user_id)
        )
        """
    )


def downgrade() -> None:
    """Drop event_rsvps and events."""
    op.execute("DROP TABLE IF EXISTS event_rsvps")
    op.execute("DROP TABLE IF EXISTS events")
