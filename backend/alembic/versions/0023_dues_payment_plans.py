"""Dues payment plans: track member installment plans against a dues cycle (board card c195).

TWO NEW TABLES.

`dues_payment_plans` is one row per member's plan for one cycle: total_cents (must
equal the cycle's amount_cents — enforced at the route, not here, since that check
needs the cycle row), installment_count, and a status that starts 'active' and ends
'completed' (every installment paid) or 'canceled' (treasurer/president called it
off). AT MOST ONE ACTIVE PLAN PER MEMBER PER CYCLE is enforced the same way c51's
uq_dues_intent_live and c83's uq_role_terms_open_per_membership enforce their own
"at most one live X" invariants: a PARTIAL UNIQUE INDEX on (dues_cycle_id, user_id)
WHERE status = 'active', not a read-check-then-insert in the route. A member who
already has an active plan loses the race for a second one at the database.

`dues_plan_installments` is the schedule: seq, amount_cents, due_date, and — once
paid — paid_at plus the ledger_entry_id it was recorded against. ON DELETE CASCADE
on plan_id, because an installment has no meaning once its plan is gone. UNIQUE
(plan_id, seq) so a plan cannot end up with two rows claiming the same slot.

THE LEDGER ENTRY_TYPE CHECK GAINS 'dues_installment'. Recording a paid installment
appends its own ledger row rather than reusing 'dues_payment', because
uq_ledger_dues_payment_once (migration 0010, board c51/c172) already means "at most
one dues_payment row per (cycle, member), EVER" — an installment plan needs to post
several payments for the same cycle/member over time, which that constraint exists
specifically to forbid for 'dues_payment'. A new entry_type sidesteps it rather than
weakening it.

THE REAL CONSTRAINT NAME IS NOT WHAT models/finance.py DECLARES, same drift 0019 hit
on posts.audience and 0022 hit on content_reports/moderation_actions.target_type:
ledger_entries.entry_type was created as an inline, unnamed column CHECK back in
0001 (`entry_type TEXT NOT NULL CHECK (entry_type IN (...))`), so Postgres
auto-named it `ledger_entries_entry_type_check` — not `ck_ledger_entries_entry_type`,
the name the model has always claimed and that was never realized in any real
schema. Verified fresh against this branch's own test database's catalog
(pg_constraint via psql), not assumed from the model or from the other two
migrations' notes. Dropped by the REAL name, recreated under the DECLARED one —
both widening the allowed values and closing the drift permanently, the same move
0019 and 0022 already made for their own constraints.

Postgres has no ALTER-constraint-in-place for a plain CHECK, so this is a
drop-and-recreate. Safe against live data: every existing row is already one of the
five original values, all still legal, so the recreate cannot fail on the way up.

Chains off 0022, the current single head (verified with `alembic heads`).
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# The constraint's real, Postgres-generated name (0001's inline unnamed CHECK) vs.
# the name models/finance.py has always declared. See module docstring.
_ENTRY_TYPE_REAL_NAME = "ledger_entries_entry_type_check"
_ENTRY_TYPE_DECLARED_NAME = "ck_ledger_entries_entry_type"
_OLD_ENTRY_TYPES = "'dues_payment','expense','budget_allocation','correction','payout'"
_NEW_ENTRY_TYPES = _OLD_ENTRY_TYPES + ",'dues_installment'"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dues_payment_plans (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id        UUID NOT NULL REFERENCES chapters(id),
            dues_cycle_id     UUID NOT NULL REFERENCES dues_cycles(id),
            user_id           UUID NOT NULL REFERENCES users(id),
            total_cents       INTEGER NOT NULL,
            installment_count INTEGER NOT NULL,
            status            TEXT NOT NULL DEFAULT 'active'
                              CONSTRAINT ck_dues_payment_plans_status
                              CHECK (status IN ('active', 'completed', 'canceled')),
            note              TEXT,
            created_by        UUID NOT NULL REFERENCES users(id),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # THE invariant: at most one LIVE plan per member per cycle. A second
    # create-plan call for the same (cycle, member) loses to the database here,
    # not to a check-then-insert in the route — same shape as uq_dues_intent_live
    # (c51) and uq_role_terms_open_per_membership (c83).
    op.execute(
        "CREATE UNIQUE INDEX uq_dues_payment_plans_active_per_member "
        "ON dues_payment_plans (dues_cycle_id, user_id) WHERE status = 'active'"
    )

    op.execute(
        """
        CREATE TABLE dues_plan_installments (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id         UUID NOT NULL REFERENCES dues_payment_plans(id) ON DELETE CASCADE,
            seq             INTEGER NOT NULL,
            amount_cents    INTEGER NOT NULL,
            due_date        DATE NOT NULL,
            paid_at         TIMESTAMPTZ,
            ledger_entry_id UUID REFERENCES ledger_entries(id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_dues_plan_installments_seq "
        "ON dues_plan_installments (plan_id, seq)"
    )

    # Drop by the name the database actually has (verified against pg_constraint),
    # not the one models/finance.py declares — see module docstring.
    op.execute(f"ALTER TABLE ledger_entries DROP CONSTRAINT {_ENTRY_TYPE_REAL_NAME}")
    op.execute(
        f"ALTER TABLE ledger_entries ADD CONSTRAINT {_ENTRY_TYPE_DECLARED_NAME} "
        f"CHECK (entry_type IN ({_NEW_ENTRY_TYPES}))"
    )


def downgrade() -> None:
    # Any row already written as 'dues_installment' would violate the narrower
    # constraint below — deliberate, same reasoning as 0019's downgrade: a
    # downgrade of a type that stopped existing should fail loud rather than
    # silently strand or reclassify real ledger rows.
    op.execute(f"ALTER TABLE ledger_entries DROP CONSTRAINT {_ENTRY_TYPE_DECLARED_NAME}")
    op.execute(
        f"ALTER TABLE ledger_entries ADD CONSTRAINT {_ENTRY_TYPE_REAL_NAME} "
        f"CHECK (entry_type IN ({_OLD_ENTRY_TYPES}))"
    )

    op.execute("DROP TABLE IF EXISTS dues_plan_installments")
    op.execute("DROP TABLE IF EXISTS dues_payment_plans")
