"""Cross-table dues guard: close the reservation/plan TOCTOU (board cards c224, c230).

THE HOLE, confirmed independently by two adversarial-review lenses plus a dedicated
refuter on c224's wave-2 sweep. payments.py's create_dues_payment_intent and
finance.py's create_dues_payment_plan each guard against their OWN table exactly the
way this repo always has — a read that picks the honest 409 reason, backed by a
partial unique index that is the REAL guard under concurrency:
uq_dues_intent_live (migration 0010) for a live reservation, and
uq_dues_payment_plans_active_per_member (migration 0023) for an active plan. Each
route ALSO reads the OTHER table before inserting (on_payment_plan / payment_in_progress),
but that read is a plain check-then-act — nothing constrains dues_payment_intents
against dues_payment_plans the way each table constrains itself. db.py runs plain
READ COMMITTED (no isolation override), and the reservation path has real network
I/O (Stripe) between its own checks and its insert. A member tapping Pay at the same
moment a treasurer creates their plan can land both guards' reads before either
insert commits — both pass, both commit, and a live reservation now sits next to an
active plan: the webhook later appends a full dues_payment while installments also
land on the same cycle, genuine double collection. This is the one seam on the
dues money path where "the read only picks the reason, the index is the guard" was
never actually built — because the guard it needed does not fit in an index. A
single-table partial unique index cannot express "no row in THIS table may be live
while a row in THAT OTHER table is live" — that is an invariant across two tables,
which Postgres has no index type for. A trigger is the mechanism that CAN reach
across tables, so that is what this migration adds — not a novelty, just the same
"the database is the guard" convention this repo has followed since 0001's
ledger_append_only, applied to the one invariant an index cannot hold.

THE MECHANISM. Two BEFORE ROW triggers, one per table, INSERT and UPDATE OF status:

- cross_table_dues_guard_intents_fn(), on dues_payment_intents: a row entering
  'open' or 'succeeded' (uq_dues_intent_live's own definition of "live") RAISEs if
  an ACTIVE dues_payment_plans row already exists for the same
  (dues_cycle_id, user_id).
- cross_table_dues_guard_plans_fn(), on dues_payment_plans: a row entering 'active'
  RAISEs if a LIVE ('open' or 'succeeded') dues_payment_intents row already exists
  for the same pair.

Both triggers skip entirely — no lock, no check — when NEW's incoming status is not
the live one, and ALSO skip on an UPDATE where OLD's status was ALREADY live: that
second guard is what keeps c234's own resolution paths (a reservation settling
open -> succeeded, or moving to failed/canceled; a plan completing or being
canceled) from re-running this check on every ordinary status transition. A
transition INTO the live state is the only one that can newly violate the
invariant; the invariant was already enforced at the moment a row FIRST became
live, and stays enforced for as long as it stays live under a status that was
live before too.

THE RACE ITSELF NEEDS MORE THAN THE EXISTS CHECK. Postgres's default READ COMMITTED
means each statement's SELECT sees only rows some OTHER transaction has already
committed — so the trigger's own EXISTS, run naively, is exactly as racy as the
route-level read it is meant to backstop: two transactions can both run their
EXISTS before either commits, both see nothing, both proceed. What actually closes
this is a transaction-scoped advisory lock — pg_advisory_xact_lock, keyed on
hashtext(dues_cycle_id) and hashtext(user_id), taken BEFORE the EXISTS, inside
BOTH trigger functions, with the IDENTICAL two-part key for a given pair on both
sides. The second transaction to reach this lock for the same pair blocks until
the first commits or rolls back; by the time it resumes, whatever the first
transaction did is now either committed (visible to the blocked transaction's
EXISTS) or gone (rolled back). One xact-scoped lock per pair, always acquired in
the same two-argument order on both sides, so there is only ever one key in play
per pair and never two locks taken in opposite orders — this cannot deadlock
against its own counterpart. This is what actually makes the two routes'
concurrent inserts INTERLEAVE rather than run blind, which is the property the
EXISTS check alone could not provide.

WHY A TRIGGER OVER SERIALIZABLE ISOLATION. Postgres SERIALIZABLE would also catch
this — but only by moving the ENTIRE dues money path (both routes, every session
touching either table) onto SERIALIZABLE with app-wide retry-on-40001 handling,
a blast radius covering code this card was not asked to touch and a failure mode
(retryable serialization errors on ordinary, non-conflicting requests) this
codebase has no plumbing for today. A trigger scoped to exactly the two tables and
exactly the two transitions that can violate this one invariant is the smaller,
provably-equivalent fix — same reasoning 0025 gave for a delta trigger over a
broader change to every vote-writing path.

WHY BEFORE ROW, NOT AFTER (unlike 0025's chirp_vote_score). 0025 needed the row to
already exist to compute a delta against it. This trigger needs nothing from the
row's own existence — it is a pure guard — so BEFORE lets a doomed insert/update
raise before Postgres ever writes the tuple, rather than writing it and unwinding
the write when the exception aborts the statement anyway.

WHY A SINGLE SHARED LOCK KEY BEFORE A SINGLE SHARED TABLE LOCK. The key is scoped
to (dues_cycle_id, user_id) rather than one lock for the whole guard, so two
DIFFERENT members' concurrent dues actions on the same cycle never queue behind
each other — only two writes for the SAME member on the SAME cycle ever contend,
which is the actual size of the invariant being protected.

THE MARKER. The RAISE carries ERRCODE 'P0001' (plpgsql's own default for a plain
RAISE EXCEPTION - stated explicitly here rather than left implicit, so a reader
does not have to know that default to know what is being raised) and a MESSAGE
containing the literal string 'cross_table_dues_guard', which is the ONLY thing
that string is for: payments.py's reservation insert and finance.py's plan insert
each catch the resulting error and grep for that marker
(app.core.errors.is_cross_table_dues_guard_conflict) to turn it into the exact
same honest 409 their own read-guard would have given a moment earlier
(on_payment_plan / payment_in_progress) - the trigger is the backstop, the read
stays the fast, honest-reason path, same division of labor 0010's own docstring
describes for uq_dues_intent_live.

NAMED EXPLICITLY, dropped by the same names (this repo's own convention after 0019
and 0023 hit real-vs-declared name drift): cross_table_dues_guard_intents /
cross_table_dues_guard_plans for the triggers, _fn suffixed for their functions,
one pair per table, no ambiguity between the two catalogs.

Chains off 0027, the verified head (`alembic heads`, run before this file existed).
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION cross_table_dues_guard_intents_fn() RETURNS trigger AS $$
        BEGIN
            -- Not entering a LIVE state (uq_dues_intent_live's own definition:
            -- 'open' or 'succeeded') cannot newly violate the invariant. Every
            -- c234 resolution to 'failed'/'canceled' exits here, lock-free.
            IF NEW.status NOT IN ('open', 'succeeded') THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('open', 'succeeded') THEN
                -- Already live before this row changed (open -> succeeded
                -- settlement, or any other same-liveness update) — this row was
                -- already checked the moment it FIRST became live, and the
                -- invariant it established then still holds. Re-running the
                -- check here would take the lock on every settlement for no
                -- new information.
                RETURN NEW;
            END IF;

            -- SEE MODULE DOCSTRING: READ COMMITTED's EXISTS below only sees
            -- committed rows, so this lock is what forces two transactions
            -- racing the SAME (cycle, member) pair to interleave rather than
            -- both pass blind. Keyed identically (same two hashtext() args, same
            -- order) in cross_table_dues_guard_plans_fn() below, so both sides of
            -- this invariant queue on the ONE key for a given pair.
            PERFORM pg_advisory_xact_lock(
                hashtext(NEW.dues_cycle_id::text), hashtext(NEW.user_id::text)
            );

            IF EXISTS (
                SELECT 1 FROM dues_payment_plans
                 WHERE dues_cycle_id = NEW.dues_cycle_id
                   AND user_id = NEW.user_id
                   AND status = 'active'
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0001',
                    MESSAGE = format(
                        'cross_table_dues_guard: an active dues_payment_plan '
                        'already exists for dues_cycle_id=%s, user_id=%s — a '
                        'self-serve reservation cannot go live at the same time '
                        '(board c230)',
                        NEW.dues_cycle_id, NEW.user_id
                    );
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION cross_table_dues_guard_plans_fn() RETURNS trigger AS $$
        BEGIN
            IF NEW.status <> 'active' THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'active' THEN
                RETURN NEW;
            END IF;

            -- Same key, same order, as cross_table_dues_guard_intents_fn() above —
            -- see that function's comment and the module docstring.
            PERFORM pg_advisory_xact_lock(
                hashtext(NEW.dues_cycle_id::text), hashtext(NEW.user_id::text)
            );

            IF EXISTS (
                SELECT 1 FROM dues_payment_intents
                 WHERE dues_cycle_id = NEW.dues_cycle_id
                   AND user_id = NEW.user_id
                   AND status IN ('open', 'succeeded')
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0001',
                    MESSAGE = format(
                        'cross_table_dues_guard: a live dues_payment_intent '
                        'reservation already exists for dues_cycle_id=%s, '
                        'user_id=%s — a payment plan cannot go active at the '
                        'same time (board c230)',
                        NEW.dues_cycle_id, NEW.user_id
                    );
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE TRIGGER cross_table_dues_guard_intents
        BEFORE INSERT OR UPDATE OF status ON dues_payment_intents
        FOR EACH ROW EXECUTE FUNCTION cross_table_dues_guard_intents_fn()
        """
    )
    op.execute(
        """
        CREATE TRIGGER cross_table_dues_guard_plans
        BEFORE INSERT OR UPDATE OF status ON dues_payment_plans
        FOR EACH ROW EXECUTE FUNCTION cross_table_dues_guard_plans_fn()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS cross_table_dues_guard_plans ON dues_payment_plans")
    op.execute("DROP TRIGGER IF EXISTS cross_table_dues_guard_intents ON dues_payment_intents")
    op.execute("DROP FUNCTION IF EXISTS cross_table_dues_guard_plans_fn()")
    op.execute("DROP FUNCTION IF EXISTS cross_table_dues_guard_intents_fn()")
