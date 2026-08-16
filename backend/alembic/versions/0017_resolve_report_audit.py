"""Let moderation_actions record a report being resolved (board card c91).

c91 adds PATCH /moderation/reports/{id}, which closes a report as actioned or
dismissed. Following c76's split — mutable current state on the row, append-only
history in moderation_actions — that route writes an audit row so "who dismissed this,
and why" survives a later moderator actioning the same target.

Two CHECK constraints written by 0011 refuse that row:

    action      IN ('suspend_user', 'unsuspend_user', 'remove_content')
    target_type IN ('user', 'yak', 'post', 'comment')

so the insert failed with an IntegrityError. Worth recording HOW that was found: the
route and its tests were written first, and three of the seven tests failed on the
constraint. A narrower test set — one happy path plus the authorization cases — would
have passed the 403/404/422 tests and shipped a resolve endpoint that 500s the first
time a real moderator uses it, because the only tests that touch the audit write are
the ones that actually resolve something.

This widens both constraints rather than reusing an existing value. 'remove_content'
would have fit the action column without a migration, and it would have been a lie:
dismissing a report removes nothing, and the audit log is the one place that must not
blur what happened. Same for target_type — a report is not the content it points at.

A TRAP WORTH NAMING, because it is the same family as c93: 0011 declared both CHECKs
inline in a CREATE TABLE, so Postgres auto-named them `moderation_actions_action_check`
and `moderation_actions_target_type_check` — NOT the `ck_`-prefixed names the rest of
this codebase uses by convention. A `DROP CONSTRAINT IF EXISTS ck_...` would have
succeeded while dropping nothing, then the ADD would have failed on a duplicate name,
or worse, on a differently-named table it would have left the old constraint in force
and reported success. The names below were read out of pg_constraint, not assumed.

Postgres cannot ALTER a CHECK in place, so each is dropped and recreated. Both are
additive supersets of the old values, so every existing row still satisfies them and
no data is touched.

DOWNGRADE narrows them back, which is only safe while no row uses the new values. It
deletes those rows first — deliberately, and only in the down path: leaving them would
make the downgrade fail with a constraint violation on data this migration's own
upgrade made legal, which is a worse failure than losing audit rows for a feature
being rolled back.
"""

from alembic import op

revision = "0017"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE moderation_actions DROP CONSTRAINT IF EXISTS moderation_actions_action_check")
    op.execute(
        """
        ALTER TABLE moderation_actions
        ADD CONSTRAINT moderation_actions_action_check
        CHECK (action IN ('suspend_user', 'unsuspend_user', 'remove_content', 'resolve_report'))
        """
    )
    op.execute(
        "ALTER TABLE moderation_actions DROP CONSTRAINT IF EXISTS moderation_actions_target_type_check"
    )
    op.execute(
        """
        ALTER TABLE moderation_actions
        ADD CONSTRAINT moderation_actions_target_type_check
        CHECK (target_type IN ('user', 'yak', 'post', 'comment', 'report'))
        """
    )


def downgrade() -> None:
    # See the docstring: rows using the widened values must go before the narrower
    # constraint can be re-added, or the downgrade fails on data the upgrade allowed.
    op.execute(
        "DELETE FROM moderation_actions WHERE action = 'resolve_report' OR target_type = 'report'"
    )
    op.execute("ALTER TABLE moderation_actions DROP CONSTRAINT IF EXISTS moderation_actions_action_check")
    op.execute(
        """
        ALTER TABLE moderation_actions
        ADD CONSTRAINT moderation_actions_action_check
        CHECK (action IN ('suspend_user', 'unsuspend_user', 'remove_content'))
        """
    )
    op.execute(
        "ALTER TABLE moderation_actions DROP CONSTRAINT IF EXISTS moderation_actions_target_type_check"
    )
    op.execute(
        """
        ALTER TABLE moderation_actions
        ADD CONSTRAINT moderation_actions_target_type_check
        CHECK (target_type IN ('user', 'yak', 'post', 'comment'))
        """
    )
