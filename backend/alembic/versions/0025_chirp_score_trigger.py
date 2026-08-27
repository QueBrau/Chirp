"""Maintain chirps.score with a delta trigger instead of a full re-SUM (board card c206).

THE CODE THIS REPLACES WAS CORRECT, which is why it survived review: vote_chirp ran
`UPDATE chirps SET score = (SELECT SUM(value) FROM chirp_votes WHERE chirp_id = ...)`,
so the stored score could never drift from the votes justifying it. The problem is the
shape, not the answer. A chirp with n votes costs O(n) per vote and O(n^2) across its
life, and the exclusive lock on the chirps row is held for the whole scan - so every
concurrent voter on a popular chirp queues behind a scan that gets longer with every
vote. It lands hardest on exactly the content the product most wants to succeed.

A TRIGGER RATHER THAN THE SAME DELTA IN THE ROUTER. This repo already made this call
once, in 0001, with ledger_append_only. An application-side delta is correct only for
as long as every write path remembers to apply it - and that set includes paths that do
not exist yet: a moderation tool that deletes votes, a backfill script, somebody in
psql. Driving the counter from the source of truth makes drift structurally impossible
rather than a discipline, which is the same argument the composite FKs and the
one-ballot-per-week primary key are already built on.

THE BACKFILL IS NOT DECORATION. Scores are recomputed once here, in the same
transaction that installs the trigger. If any drift is already sitting in production -
from a crashed request between the vote flush and the score update, say - this is the
one moment it can be healed. Installing the trigger without recomputing would freeze
whatever error exists today and then maintain it faithfully forever.

WHAT THIS DOES NOT DO. Concurrent votes on ONE chirp still serialise on that chirp's
row lock; that is inherent to keeping a counter in the row and no trigger removes it.
What changes is how long the lock is held (an arithmetic update rather than an index
scan) and that the cost stops growing with vote count. Genuinely lock-free counting
needs sharded counter rows or an append-only delta table summed on a schedule - real
options, deliberately out of scope, and named on c206 so nobody adds one quietly.

Number claimed as 0025 on c206 BEFORE this file was written, per CLAUDE.md. Chains off
0024, the verified head (`alembic heads`).
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Heal any existing drift BEFORE the trigger takes over, so the counter the trigger
    # starts maintaining is the correct one. See the module docstring.
    op.execute(
        """
        UPDATE chirps c
           SET score = COALESCE(
                   (SELECT SUM(v.value) FROM chirp_votes v WHERE v.chirp_id = c.id), 0
               )
        """
    )

    # RETURN NULL because this is an AFTER trigger: the return value is ignored, and
    # saying so beats returning a row that reads as though it were meaningful.
    op.execute(
        """
        CREATE FUNCTION chirp_vote_apply_score_delta() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE chirps SET score = score + NEW.value WHERE id = NEW.chirp_id;

            ELSIF TG_OP = 'UPDATE' THEN
                -- chirp_id is part of the primary key and nothing in the app moves a
                -- vote between chirps, so the branch below cannot currently fire. It
                -- exists because a counter that is silently wrong is the exact failure
                -- this trigger was written to make impossible, and "cannot happen" is
                -- a claim about today's callers rather than about the table.
                IF NEW.chirp_id IS DISTINCT FROM OLD.chirp_id THEN
                    UPDATE chirps SET score = score - OLD.value WHERE id = OLD.chirp_id;
                    UPDATE chirps SET score = score + NEW.value WHERE id = NEW.chirp_id;
                ELSIF NEW.value IS DISTINCT FROM OLD.value THEN
                    -- Flipping -1 to +1 is a delta of 2, not 1. Writing it as an
                    -- explicit subtract-then-add keeps that arithmetic visible.
                    UPDATE chirps
                       SET score = score + NEW.value - OLD.value
                     WHERE id = NEW.chirp_id;
                END IF;

            ELSIF TG_OP = 'DELETE' THEN
                -- Chirp removal is SOFT (removed_at), so votes are not cascaded away in
                -- normal operation and this branch is for direct deletes. If the chirp
                -- row is already gone the UPDATE simply matches nothing, which is the
                -- correct outcome rather than an error.
                UPDATE chirps SET score = score - OLD.value WHERE id = OLD.chirp_id;
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE TRIGGER chirp_vote_score
        AFTER INSERT OR UPDATE OR DELETE ON chirp_votes
        FOR EACH ROW EXECUTE FUNCTION chirp_vote_apply_score_delta()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS chirp_vote_score ON chirp_votes")
    op.execute("DROP FUNCTION IF EXISTS chirp_vote_apply_score_delta()")
    # Scores are left as the trigger last set them, and that is correct rather than
    # lazy: they are accurate at this instant. Downgrading only means the application
    # becomes responsible for maintaining them again - which, on the code this
    # downgrade exists to return to, it does by recomputing on the next vote anyway.
