"""Stripe dues plumbing: per-chapter customers, webhook dedup, replay-proof ledger index.

The unique partial index on ledger_entries.stripe_payment_intent_id is the load-bearing
piece. Stripe retries webhooks for days and the ledger has no UPDATE/DELETE path, so a
replayed payment_intent.succeeded would otherwise append a second permanent dues payment.
A DB constraint is required here — a check-then-insert in the handler races itself.
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE chapter_stripe_customers (
            user_id            UUID NOT NULL REFERENCES users(id),
            chapter_id         UUID NOT NULL REFERENCES chapters(id),
            stripe_customer_id TEXT NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, chapter_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE processed_stripe_events (
            event_id     TEXT PRIMARY KEY,
            event_type   TEXT NOT NULL,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ledger_stripe_payment_intent "
        "ON ledger_entries (stripe_payment_intent_id) "
        "WHERE stripe_payment_intent_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_ledger_stripe_payment_intent")
    op.execute("DROP TABLE IF EXISTS processed_stripe_events")
    op.execute("DROP TABLE IF EXISTS chapter_stripe_customers")
