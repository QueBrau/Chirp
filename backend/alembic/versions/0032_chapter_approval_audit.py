"""Let moderation_actions record chapter approvals and revocations (board card c337).

c325 added PATCH /chapters/{id}/moderation-approval and deliberately wrote NO audit row:
both CHECK constraints on moderation_actions refused the shape. As of 0031 they read

    moderation_actions_action_check     action IN ('suspend_user', 'unsuspend_user',
                                                   'remove_content', 'resolve_report')
    ck_moderation_actions_target_type   target_type IN ('user', 'chirp', 'post',
                                                        'comment', 'report')

and a platform admin approving a real org is exactly the kind of decision an audit log
exists to keep. This widens both, in 0017's shape.

TWO ACTIONS, NOT ONE WITH A FLAG. approve_chapter and revoke_chapter are distinct values
rather than one chapter_approval whose direction lives elsewhere, for the reason 0017
refused to reuse remove_content for a dismissal: the audit log is the one place that
must not blur what happened, and this table has no details column to hide a direction
in anyway. A reader of `action` alone must know whether an org was let in or shut out.

THE NAMES, read out of pg_constraint rather than assumed (0017's trap, restated because
the answer has MOVED since 0017 wrote it down): 0011 created both CHECKs inline, so
Postgres auto-named them moderation_actions_action_check and
moderation_actions_target_type_check. 0017 kept those names. 0022 then dropped the
target_type one and recreated it under the codebase's ck_ convention as
ck_moderation_actions_target_type, while the action constraint kept its auto-name — so
today one table's two constraints follow two conventions. Each DROP below names BOTH
candidates with IF EXISTS, so this migration cannot "succeed" by dropping nothing, and
each ADD keeps the name the database currently has, so nothing else (0022's downgrade,
the model's metadata) is silently renamed out from under it.
tests/test_c337_migration_widening.py pins both names before and after.

Postgres cannot ALTER a CHECK in place, so each is dropped and recreated. Both new sets
are supersets of the old ones, so every existing row satisfies them and no data moves.

DOWNGRADE REFUSES RATHER THAN DELETES. 0017's downgrade deleted the rows its own upgrade
had made legal. This one does not: moderation_actions is append-only by design (0011),
and a chapter approval is a platform admin's decision about a real organisation, not a
feature flag's side effect. If any row uses the widened values, the downgrade raises
BEFORE touching a constraint, names the count, and leaves schema and data exactly as
they were; an operator who truly means to roll back deletes those rows on purpose first.
Only a table with no such rows narrows back.
"""

from alembic import op
from sqlalchemy import text

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

ACTION_NAME = "moderation_actions_action_check"
TARGET_TYPE_NAME = "ck_moderation_actions_target_type"
# Every name either constraint has carried since 0011, dropped IF EXISTS in turn, so a
# stale name cannot leave the old CHECK in force behind a "successful" migration.
_ACTION_NAMES = (ACTION_NAME, "ck_moderation_actions_action")
_TARGET_TYPE_NAMES = (TARGET_TYPE_NAME, "moderation_actions_target_type_check")

OLD_ACTIONS = "'suspend_user', 'unsuspend_user', 'remove_content', 'resolve_report'"
NEW_ACTIONS = OLD_ACTIONS + ", 'approve_chapter', 'revoke_chapter'"
OLD_TARGET_TYPES = "'user', 'chirp', 'post', 'comment', 'report'"
NEW_TARGET_TYPES = OLD_TARGET_TYPES + ", 'chapter'"


def _replace(names: tuple[str, ...], keep: str, column: str, values: str) -> None:
    for name in names:
        op.execute(f"ALTER TABLE moderation_actions DROP CONSTRAINT IF EXISTS {name}")
    op.execute(
        f"ALTER TABLE moderation_actions ADD CONSTRAINT {keep} CHECK ({column} IN ({values}))"
    )


def upgrade() -> None:
    _replace(_ACTION_NAMES, ACTION_NAME, "action", NEW_ACTIONS)
    _replace(_TARGET_TYPE_NAMES, TARGET_TYPE_NAME, "target_type", NEW_TARGET_TYPES)


def downgrade() -> None:
    # Checked FIRST, before any DDL, so a refusal leaves nothing half-done regardless of
    # how the surrounding transaction is configured.
    offending = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM moderation_actions "
                "WHERE action IN ('approve_chapter', 'revoke_chapter') OR target_type = 'chapter'"
            )
        )
        .scalar_one()
    )
    if offending:
        raise RuntimeError(
            f"c337: refusing to downgrade 0032 - {offending} moderation_actions row(s) use "
            "approve_chapter/revoke_chapter or target_type 'chapter'. A rollback does not "
            "delete audit rows; remove them deliberately first if you really mean it."
        )
    _replace(_ACTION_NAMES, ACTION_NAME, "action", OLD_ACTIONS)
    _replace(_TARGET_TYPE_NAMES, TARGET_TYPE_NAME, "target_type", OLD_TARGET_TYPES)
