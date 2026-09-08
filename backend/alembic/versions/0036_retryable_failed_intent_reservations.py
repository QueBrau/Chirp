"""Retain failed-but-retryable Stripe intents as held reservations (c387).

Conflicting historical obligations require provider reconciliation. This migration
never selects a winning intent, changes financial rows, or calls a provider.
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def _guards(held: str) -> None:
    # Keep the c230 advisory lock key and existing trigger names. Both functions
    # use the same definition of held, including transitions within that set.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION cross_table_dues_guard_intents_fn()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status NOT IN ({held}) THEN RETURN NEW; END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ({held}) THEN RETURN NEW; END IF;
            PERFORM pg_advisory_xact_lock(
                hashtext(NEW.dues_cycle_id::text), hashtext(NEW.user_id::text)
            );
            IF EXISTS (
                SELECT 1 FROM dues_payment_plans
                 WHERE dues_cycle_id = NEW.dues_cycle_id
                   AND user_id = NEW.user_id AND status = 'active'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE =
                    'cross_table_dues_guard: active payment plan conflicts with held intent';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION cross_table_dues_guard_plans_fn()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status <> 'active' THEN RETURN NEW; END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'active' THEN RETURN NEW; END IF;
            PERFORM pg_advisory_xact_lock(
                hashtext(NEW.dues_cycle_id::text), hashtext(NEW.user_id::text)
            );
            IF EXISTS (
                SELECT 1 FROM dues_payment_intents
                 WHERE dues_cycle_id = NEW.dues_cycle_id
                   AND user_id = NEW.user_id AND status IN ({held})
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE =
                    'cross_table_dues_guard: held intent conflicts with active payment plan';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)


def _index(held: str) -> None:
    op.execute("DROP INDEX uq_dues_intent_live")
    op.execute(f"""
        CREATE UNIQUE INDEX uq_dues_intent_live
        ON dues_payment_intents (dues_cycle_id, user_id)
        WHERE status IN ({held})
    """)


def upgrade() -> None:
    # Stop concurrent writes from changing the preflight result before the index
    # and both predicates change. A busy deployment fails immediately; retry in
    # the coordinated money-writer pause instead of waiting behind live traffic.
    op.execute("LOCK TABLE dues_payment_intents, dues_payment_plans IN ACCESS EXCLUSIVE MODE NOWAIT")
    op.execute("""
        DO $$
        DECLARE duplicate_pairs bigint; plan_pairs bigint;
        BEGIN
            SELECT count(*) INTO duplicate_pairs FROM (
                SELECT dues_cycle_id, user_id FROM dues_payment_intents
                WHERE status IN ('open', 'failed', 'succeeded')
                GROUP BY dues_cycle_id, user_id HAVING count(*) > 1
            ) conflicting_intents;
            SELECT count(*) INTO plan_pairs FROM (
                SELECT DISTINCT i.dues_cycle_id, i.user_id
                FROM dues_payment_intents i JOIN dues_payment_plans p
                  ON p.dues_cycle_id = i.dues_cycle_id AND p.user_id = i.user_id
                WHERE i.status IN ('open', 'failed', 'succeeded') AND p.status = 'active'
            ) conflicting_plans;
            IF duplicate_pairs > 0 OR plan_pairs > 0 THEN
                RAISE EXCEPTION
                    'c387 reconciliation required: % obligations have multiple held intents; % obligations have an active plan with a held intent. Reconcile provider outcomes before retrying this migration.',
                    duplicate_pairs, plan_pairs USING ERRCODE = '23514';
            END IF;
        END;
        $$
    """)
    held = "'open', 'failed', 'succeeded'"
    _index(held)
    _guards(held)


def downgrade() -> None:
    # Restores old semantics, which are not safe for retryable failed intents.
    # Preserve every row; operators must keep payment writers paused on rollback.
    op.execute("LOCK TABLE dues_payment_intents, dues_payment_plans IN ACCESS EXCLUSIVE MODE NOWAIT")
    held = "'open', 'succeeded'"
    _index(held)
    _guards(held)
