"""Forbid self-blocks in the database (board card c237).

THE BUG. POST /moderation/blocks accepted the caller's own id, while
POST /moderation/blocks/by-chirp had always refused it. A stored self-block does
not affect contact - blockers_of filters subject_id out - but feed.py's c35
anti-join hides posts whose author the caller has blocked and does NOT exempt the
caller, so the row silently takes the user's own posts off their own feed. Proven
end to end before this was written: with the route guard removed, a user's own
post went visible True -> False across a self-block.

The route now refuses it at both endpoints. This is the same rule at the level
that cannot be routed around - a direct insert, a future endpoint, a fixture, a
psql session.

WHY THE DELETE IS NOT DEAD CODE, and must not be removed as such. At the time
this was written the manager read prod through the Cloud SQL proxy: user_blocks
held ZERO self-block rows and zero rows in total, so the DELETE is a no-op today
and ADD CONSTRAINT would have succeeded without it. It stays because the route
guard is NOT YET DEPLOYED when this migration runs: the house order is migrate
first, then deploy, so any self-block created between now and the redeploy would
be written by the still-unguarded route and would make ADD CONSTRAINT fail
outright. The DELETE is a forward guard against that window, not cleanup of a
known mess.

THE DEPLOY WINDOW, accepted deliberately rather than engineered away. Between
this migration landing and the redeploy finishing, the constraint exists while
the unguarded route is still live, so a self-block attempt in that window raises
the CHECK and surfaces as a 500 rather than the clean 403. The window is minutes
and the behavior is still fail-closed - the row does not get written either way.
Reversing the order would be worse: the DELETE would run against a table the old
code can still write to. If someone reports a mystery 500 on POST
/moderation/blocks around a deploy, this is it.
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Must precede ADD CONSTRAINT: a single surviving self-block row makes it fail.
    op.execute("DELETE FROM user_blocks WHERE blocker_id = blocked_id")
    op.create_check_constraint(
        "ck_user_blocks_no_self_block",
        "user_blocks",
        "blocker_id <> blocked_id",
    )


def downgrade() -> None:
    # Deliberately does not restore deleted rows: they were the bug, and a self-block
    # is not state anyone wants back.
    op.drop_constraint("ck_user_blocks_no_self_block", "user_blocks", type_="check")
