"""Actives-only chapter tier: posts.audience gains 'org_actives' (board card c102).

Jose's Aug 24 ruling on c102, binding: "active" means Membership.status == 'active'
- the EXISTING status flag - NOT a new pledge/member model and NOT dues-paid
standing (which would couple this to Stripe/c11). A chapter's content now splits
into two tiers riding the SAME audience column c34/0009 already uses to split org
vs campus: 'org' stays the chapter-public tier (any non-removed member sees it,
unchanged), and 'org_actives' is the new tier layered on top of it - chapter-scoped
exactly like 'org', but only returned to a viewer whose OWN membership in that
chapter currently has status='active'. Gated server-side in the feed query itself
(routers/feed.py list_posts / _readable_post), never by fetching every post and
filtering the ones the caller can't see in Python.

A migration IS required here. The card asked to check whether audience was a plain
TEXT column with only a Pydantic Literal guarding it (in which case widening the
Literal alone would need no migration) - it is not: there is a REAL Postgres CHECK
constraint on this column, added inline by 0009 (`ALTER TABLE posts ADD COLUMN
audience ... CHECK (audience IN ('org', 'campus'))`), so an org_actives insert
would 500 on a constraint violation without this.

FOUND ALONG THE WAY, flagged loudly rather than quietly worked around: the actual
constraint's name in every database that ran 0009 is Postgres's auto-generated
`posts_audience_check` (the default name for an unnamed inline column CHECK),
NOT `ck_posts_audience` - the name models/social.py's CheckConstraint object has
always claimed. That ORM-side name was never realized in any real schema; nothing
before this migration ever needed to reference the constraint by name, so the drift
was silent. Verified directly against a live Postgres catalog (psql's describe-
table output), not assumed from the model. This migration drops the constraint
under its REAL name
and recreates it AS `ck_posts_audience`, which both adds 'org_actives' and closes
that drift permanently - the model's declared name finally matches reality.

Postgres has no ALTER-constraint-in-place for a plain CHECK, so this is a
drop-and-recreate. Safe against live data: every existing row is already 'org' or
'campus', both still legal, so the recreate can never fail the check on the way
back up.

Number claimed as 0019, NOT 0018 - 0018 already belongs to the unmerged
q/c162-live-polls branch. This chains off 0013, the actual current head on main
(migration file numbers here do NOT match chain order - e.g. 0013_campus_posts.py
has down_revision 0016, landing after it - so the head was verified with
`alembic heads` rather than assumed from the filename). Whatever 0018 chains off
on its own branch, the two will need an alembic merge revision once c162 lands;
flagged here on purpose rather than guessed at, since this branch has no
visibility into 0018's actual content.
"""
from alembic import op

revision = "0019"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE posts DROP CONSTRAINT posts_audience_check")
    op.execute(
        "ALTER TABLE posts ADD CONSTRAINT ck_posts_audience "
        "CHECK (audience IN ('org', 'campus', 'org_actives'))"
    )


def downgrade() -> None:
    # Any row already written as 'org_actives' would violate the narrower
    # constraint below - deliberate. A downgrade of a tier that stopped existing
    # is exactly the situation that should fail loud instead of silently
    # reclassifying content nobody asked to reclassify.
    op.execute("ALTER TABLE posts DROP CONSTRAINT ck_posts_audience")
    op.execute(
        "ALTER TABLE posts ADD CONSTRAINT posts_audience_check "
        "CHECK (audience IN ('org', 'campus'))"
    )
