"""Index conversation_members on user_id for active rows (board card c212).

THE MISSING INDEX. conversation_members' primary key is (conversation_id, user_id) -
conversation_id leads, so the primary key's own btree cannot serve a lookup keyed on
user_id alone; a leftmost-prefix index requires the leading column, and user_id is not
it. list_conversations (routers/messages.py) is the query this fixes:

    select(Conversation)
    .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
    .where(
        ConversationMember.user_id == user.id,
        ConversationMember.left_at.is_(None),
    )
    .order_by(Conversation.created_at.desc())

Every load of a user's messaging inbox runs `WHERE conversation_members.user_id = :id
AND left_at IS NULL`, and until this migration that predicate had no index to use, so
it sequential-scanned the whole table - on every inbox open, for every user, growing
with total membership rows rather than with any one user's conversation count.

INDEX SHAPE: partial on `user_id` WHERE `left_at IS NULL`, not a composite with a
second column. list_conversations orders by Conversation.created_at, a column on the
OTHER side of the join, not on conversation_members - so unlike c208's
(post_id, created_at) (where the sort key lives on the same table as the filter), there
is no trailing column that this table's index could contribute to the ORDER BY.
Partial rather than plain, because left_at IS NULL is exactly what the one reader that
filters on user_id filters by - the same reasoning as c208's post_comments index
(idx_post_comments_post_live, migration 0026): index the predicate readers actually
use. (The other conversation_members readers - _require_active_member's session.get by
(conversation_id, user_id), send_message's recipient fan-out keyed on conversation_id,
and the second half of list_conversations that fetches full member rows by
conversation_id.in_(...) - all lead on conversation_id, which the primary key already
serves; this migration does not touch those paths.)

PLAIN CREATE INDEX, NOT CONCURRENTLY - deliberately, unlike 0026. CONCURRENTLY buys
avoiding a write-blocking SHARE lock during the build, at the cost of not being able to
run inside a transaction (autocommit block) and of leaving an INVALID index behind on a
failed build. conversation_members is small at current scale (bounded by real
conversations x real participants, nowhere near post_comments' write-heavy growth
curve), so the SHARE lock's build-time write pause is not worth trading transactional
safety for. This migration runs as an ordinary alembic step inside the surrounding
transaction, matching every migration before 0026 in this repo.

Number claimed as 0027 on c212 before this file was written, per CLAUDE.md. Chains off
0018, the verified head (`alembic heads`) - the chain is 0013 -> 0019 -> ... -> 0026 ->
0018, not filename order.
"""

from alembic import op

revision = "0027"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_conversation_members_user_active "
        "ON conversation_members (user_id) "
        "WHERE left_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_conversation_members_user_active")
