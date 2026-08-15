"""Dues payment reservations: stop one member being charged twice for one cycle (board card c51).

The bug this closes: nothing in the database knew a payment was IN FLIGHT. A dues payment
was only recorded once its webhook settled, so during ACH's multi-day "processing" window
the cycle still read as unpaid. The Stripe idempotency key was also scoped per rail
(dues:{cycle}:{user}:{rail}), so a member who got impatient and retried on card created a
GENUINELY different PaymentIntent rather than a deduped retry. Both would succeed, and each
would append its own row to an append-only ledger with no reversal path.

Two guards, deliberately at different layers:

1. dues_payment_intents + uq_dues_intent_live — the RESERVATION. A row is inserted before
   Stripe is ever called, so the second attempt loses the race at the database rather than
   at Stripe. The partial index covers only 'open' and 'succeeded' because a genuinely
   failed or canceled payment MUST be retryable; payment_failed/canceled webhooks move a
   row out of those states and release the reservation.

2. uq_ledger_dues_payment_once — the BACKSTOP on the ledger itself. The pre-existing
   uq_ledger_stripe_payment_intent only dedups a REPLAY of one intent id; it cannot stop
   two DIFFERENT intents settling against the same obligation. This one states the actual
   business invariant: at most one dues_payment per (cycle, member).
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dues_payment_intents (
            id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id               UUID NOT NULL REFERENCES chapters(id),
            dues_cycle_id            UUID NOT NULL REFERENCES dues_cycles(id),
            user_id                  UUID NOT NULL REFERENCES users(id),
            rail                     TEXT NOT NULL CHECK (rail IN ('card', 'ach')),
            status                   TEXT NOT NULL DEFAULT 'open'
                                     CHECK (status IN ('open', 'succeeded', 'failed', 'canceled')),
            stripe_payment_intent_id TEXT,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # One live reservation per (cycle, member). 'open' = created at Stripe and not yet
    # resolved; 'succeeded' = paid. Anything else is retryable, which is why they are
    # excluded rather than the index covering every row.
    op.execute(
        "CREATE UNIQUE INDEX uq_dues_intent_live "
        "ON dues_payment_intents (dues_cycle_id, user_id) "
        "WHERE status IN ('open', 'succeeded')"
    )
    # The intent id is written after Stripe answers, so it is nullable — but once set it
    # must be unique, so a webhook can resolve exactly one reservation.
    op.execute(
        "CREATE UNIQUE INDEX uq_dues_intent_stripe_id "
        "ON dues_payment_intents (stripe_payment_intent_id) "
        "WHERE stripe_payment_intent_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ledger_dues_payment_once "
        "ON ledger_entries (dues_cycle_id, related_user_id) "
        "WHERE entry_type = 'dues_payment' "
        "AND dues_cycle_id IS NOT NULL AND related_user_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_ledger_dues_payment_once")
    op.execute("DROP INDEX IF EXISTS uq_dues_intent_stripe_id")
    op.execute("DROP INDEX IF EXISTS uq_dues_intent_live")
    op.execute("DROP TABLE IF EXISTS dues_payment_intents")
