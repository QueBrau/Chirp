"""Account suspension + audited content removal (board card c76).

The LIVE /terms already claims "We can remove content and suspend accounts that break
these rules" and "We can suspend or close an account" — this migration is what makes
that true on the server instead of rewording the claim away. Before this, no model
anywhere had a suspend/banned/is_active/disabled field, and yaks.removed_at had no
record of WHO removed it.

Two kinds of state, deliberately separate:

1. users.suspended_at / suspension_reason / suspended_by — MUTABLE current state. This
   is what get_current_user checks on every request (cheap, single-row, no join) and
   what an unsuspend clears back to NULL. Cleared state means clean history is lost on
   the columns alone, which is why moderation_actions exists.

2. moderation_actions — an APPEND-ONLY audit log (application-enforced, like the
   pattern ledger_entries uses for money, minus the DB trigger since this data isn't
   financial). Every suspend/unsuspend/content-removal writes one row here and it is
   never updated or deleted, so "who removed what, when, and why" survives a
   suspend -> unsuspend -> suspend cycle even though the users columns only ever show
   the latest state. target_type/target_id mirrors content_reports' existing shape
   rather than inventing a new targeting scheme.

posts.removed_reason / post_comments.removed_reason ride the EXISTING deleted_at
column those tables already have (feed.py already filters on it) rather than adding a
parallel "removed_at" — a moderator's removal must actually disappear from the feed,
and this router cannot touch feed.py to teach it a second column to filter on.
removed_reason NULL means a self/president delete; NOT NULL means a moderator removed
it — the two paths share deleted_at but stay distinguishable.
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN suspended_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN suspension_reason TEXT"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN suspended_by UUID REFERENCES users(id)"
    )
    op.execute(
        "ALTER TABLE posts ADD COLUMN removed_reason TEXT"
    )
    op.execute(
        "ALTER TABLE post_comments ADD COLUMN removed_reason TEXT"
    )
    op.execute(
        """
        CREATE TABLE moderation_actions (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_id    UUID NOT NULL REFERENCES users(id),
            action      TEXT NOT NULL
                        CHECK (action IN ('suspend_user', 'unsuspend_user', 'remove_content')),
            target_type TEXT NOT NULL
                        CHECK (target_type IN ('user', 'yak', 'post', 'comment')),
            target_id   UUID NOT NULL,
            reason      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Lets "show me every action ever taken on this target" resolve without a seq scan —
    # the shape a moderation-history UI (or a support ticket lookup) would query by.
    op.execute(
        "CREATE INDEX idx_moderation_actions_target ON moderation_actions (target_type, target_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_moderation_actions_target")
    op.execute("DROP TABLE IF EXISTS moderation_actions")
    op.execute("ALTER TABLE post_comments DROP COLUMN IF EXISTS removed_reason")
    op.execute("ALTER TABLE posts DROP COLUMN IF EXISTS removed_reason")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS suspended_by")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS suspension_reason")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS suspended_at")
