"""Add delivery_outbox (board card c356).

Brand-new, empty table -- there is nothing pre-existing to backfill (unlike
0033/0037/0349's careful data-preserving dances), so this is a plain create/drop.

Originally authored parented on 0037 (the single head at authoring time, with PR
#274/c390's 0038 still an open branch). #274/c390 merged before this PR did, so
per HANDOFF's migration rule this was re-pointed to down_revision='0038' right
before opening this PR, re-running `alembic heads` to confirm a single head
afterward -- see this PR's body.

kind is CHECKed to only ever be 'message' or 'poll' even though this PR only ever
inserts 'message' rows; polls stay on their own best-effort path (DELIVERY-OUTBOX.md)
until a follow-up card wires them into this table.

idx_delivery_outbox_pending is a partial index matching exactly the predicate the
sweeper's claim query filters on (delivered_at IS NULL AND dead_at IS NULL), same
"index the predicate readers actually use" precedent as
idx_conversation_members_user_active (models/messaging.py).
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE delivery_outbox (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind TEXT NOT NULL,
            recipient_ids UUID[] NOT NULL DEFAULT '{}',
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            delivered_at TIMESTAMPTZ,
            dead_at TIMESTAMPTZ,
            last_error TEXT,
            CONSTRAINT ck_delivery_outbox_kind CHECK (kind IN ('message', 'poll'))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_delivery_outbox_pending ON delivery_outbox (next_attempt_at) "
        "WHERE delivered_at IS NULL AND dead_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_delivery_outbox_pending")
    op.execute("DROP TABLE IF EXISTS delivery_outbox")
