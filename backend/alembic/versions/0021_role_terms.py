"""role_terms: role history per membership (board card c83).

Jose's Aug 24 product ruling, recorded in the board decisions log: a chapter role is
a DATED TERM, not a plain fact. memberships.role has always been the CURRENT value
with no memory of how it got there — a president who steps down and hands the gavel
to the treasurer leaves no trace of ever having been president. This migration adds
the missing history table without touching memberships.role itself, which stays the
current-role source of truth every existing reader already depends on (GET
/chapters/{id}/members, require_role, capabilities_for, ...); role_terms is additive.

SCHEMA: one row per (membership, contiguous role span). started_at/ended_at bound the
span; ended_at IS NULL means the term is still open — the role the member holds RIGHT
NOW. changed_by records who made the change; nullable, because the backfill below has
no acting user to attribute it to. ON DELETE CASCADE on membership_id — a term has no
meaning once its membership is gone.

INVARIANT, enforced in Postgres and not just at the application layer: AT MOST ONE
open term per membership. A partial unique index on membership_id WHERE ended_at IS
NULL is the actual guard. app/services/role_term_service.py closes the old open term
and opens the new one in the same transaction on every real role change, but this
index is what makes "two open terms" impossible even if that path is ever bypassed or
race-loses.

BACKFILL: every existing membership gets exactly one open term seeded from its
CURRENT memberships.role, so the invariant above holds immediately for every
membership that exists as of this migration. started_at is stamped at MIGRATION TIME
(now()) for every backfilled row — a deliberate approximation, not a discovered fact.
THE TRUE DATE A MEMBER'S CURRENT ROLE ACTUALLY BEGAN IS UNKNOWABLE FROM THE DATA THIS
SYSTEM HAS EVER RECORDED: memberships.joined_at is when they joined the CHAPTER, not
when they took on their CURRENT role, and nothing upstream of this migration ever
wrote a role-change timestamp. Every term opened AFTER this migration has an honest
started_at; these backfilled rows do not, and should read as "known to hold this role
as of chirp's role_terms rollout," never as "became this role on this date."

MIGRATION NUMBERING: claimed as 0021 on the board. Originally written against
down_revision 0019 (the head on main when the branch was cut) while board card c175
(Q) was mid-flight on 0020; the board decisions log pre-committed the manager to
re-parenting onto 0020 at merge time if c175 landed first. c175 merged to main as
0020_house_leaderboard (f34c6bd) before this branch did, so that re-parent happened
exactly as recorded: down_revision is 0020. The single-head check in CI enforces
this either way, so a wrong parent cannot merge unnoticed.
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE role_terms (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            membership_id UUID NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            role          TEXT NOT NULL,
            started_at    TIMESTAMPTZ NOT NULL,
            ended_at      TIMESTAMPTZ,
            changed_by    UUID REFERENCES users(id)
        )
        """
    )
    # Supports "history for a member, newest first" without a sequential scan.
    op.execute(
        "CREATE INDEX idx_role_terms_membership ON role_terms(membership_id, started_at DESC)"
    )
    # The actual "role is a dated term" invariant: at most one open term per membership.
    op.execute(
        "CREATE UNIQUE INDEX uq_role_terms_open_per_membership "
        "ON role_terms(membership_id) WHERE ended_at IS NULL"
    )
    # Backfill: one open term per EXISTING membership, seeded from its current role.
    # started_at = now() for every row here — see the module docstring above for why
    # a true historical start date cannot be recovered.
    op.execute(
        """
        INSERT INTO role_terms (membership_id, role, started_at, ended_at, changed_by)
        SELECT id, role, now(), NULL, NULL
        FROM memberships
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS role_terms")
