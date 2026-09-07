"""Bind Stripe settlement to the stored reservation (board card c349, audit F09).

Two changes, one purpose: at webhook time the handler must compare the event to what
WE stored when the reservation was made — never to the event's own metadata, and never
to a value that could have moved since.

1. dues_payment_intents gains amount_cents and currency, the SNAPSHOT of what the
   PaymentIntent was created for (payments.py writes them in the same transaction as
   the reservation row, before Stripe is called). Settlement compares the event's
   amount_received AND amount to this snapshot, not to dues_cycles at settlement time:
   the cycle has no edit route today, but a snapshot is right by construction rather
   than by the absence of a route. Backfill: existing rows take their cycle's amount
   through the FK join and 'usd' (the only currency this integration has ever
   created — stripe_service.CURRENCY). Then NOT NULL. If any row is left NULL the
   migration FAILS rather than guessing; the c237-class proof in
   tests/test_c349_migration_0033.py seeds a pre-migration reservation and checks the
   backfilled value.

2. stripe_settlement_quarantine, append-only. A verified event that does not match its
   reservation — no reservation, wrong livemode, wrong connected account, wrong amount
   or currency, altered metadata — writes ONE row here (with the event id also recorded
   in processed_stripe_events, so Stripe stops redelivering something that can never
   become valid) and nothing else: no ledger row, no reservation status change. The
   row carries internal ids, amounts, currency, livemode and the acct_ id — never the
   raw event, which holds customer PII. reason is CHECK-constrained so a new mismatch
   class has to be declared here, the same discipline moderation_actions.action keeps.
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

QUARANTINE_REASONS = (
    "'no_reservation'",
    "'livemode_mismatch'",
    "'livemode_unverifiable'",
    "'account_mismatch'",
    "'amount_mismatch'",
    "'currency_mismatch'",
    "'metadata_mismatch'",
)


def upgrade() -> None:
    op.execute("ALTER TABLE dues_payment_intents ADD COLUMN amount_cents INTEGER")
    op.execute("ALTER TABLE dues_payment_intents ADD COLUMN currency TEXT")
    # Backfill through the cycle FK. A reservation whose cycle is somehow missing keeps
    # NULL here and the SET NOT NULL below fails the migration loudly — by design.
    op.execute(
        """
        UPDATE dues_payment_intents AS r
        SET amount_cents = c.amount_cents, currency = 'usd'
        FROM dues_cycles AS c
        WHERE c.id = r.dues_cycle_id
        """
    )
    op.execute("ALTER TABLE dues_payment_intents ALTER COLUMN amount_cents SET NOT NULL")
    op.execute("ALTER TABLE dues_payment_intents ALTER COLUMN currency SET NOT NULL")

    op.execute(
        f"""
        CREATE TABLE stripe_settlement_quarantine (
            id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id                 TEXT NOT NULL UNIQUE,
            event_type               TEXT NOT NULL,
            stripe_payment_intent_id TEXT,
            reservation_id           UUID REFERENCES dues_payment_intents(id),
            chapter_id               UUID REFERENCES chapters(id),
            reason                   TEXT NOT NULL
                                     CHECK (reason IN ({", ".join(QUARANTINE_REASONS)})),
            expected                 JSONB NOT NULL,
            observed                 JSONB NOT NULL,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_settlement_quarantine_chapter "
        "ON stripe_settlement_quarantine (chapter_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE FUNCTION reject_settlement_quarantine_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'stripe_settlement_quarantine is append-only'
                USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER settlement_quarantine_append_only "
        "BEFORE UPDATE OR DELETE ON stripe_settlement_quarantine "
        "FOR EACH ROW EXECUTE FUNCTION reject_settlement_quarantine_mutation()"
    )


def downgrade() -> None:
    # Quarantine rows are diagnostic, not money: dropping the table on rollback loses
    # visibility, not funds (the ledger was never written for them). The snapshot
    # columns go with it; the cycle still holds the price they were copied from.
    op.execute("DROP INDEX IF EXISTS idx_settlement_quarantine_chapter")
    op.execute("DROP TABLE IF EXISTS stripe_settlement_quarantine")
    op.execute("DROP FUNCTION IF EXISTS reject_settlement_quarantine_mutation()")
    op.execute("ALTER TABLE dues_payment_intents DROP COLUMN IF EXISTS currency")
    op.execute("ALTER TABLE dues_payment_intents DROP COLUMN IF EXISTS amount_cents")
