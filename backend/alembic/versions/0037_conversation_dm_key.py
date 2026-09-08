"""Add Conversation.dm_key and its partial unique index (board c344).

THE PREMISE OF THIS CARD IS THAT DUPLICATE DMS ALREADY EXIST. Before this migration,
POST /conversations had no dedup at all: every call for kind="dm" inserted a brand-new
Conversation + ConversationMember row pair, so repeat taps on the same person (or a
retried request) already produced N rows for the same participant pair in any real
database this has run against. A naive migration that backfills dm_key on every
existing dm row unconditionally, THEN creates the unique index, throws a duplicate-key
error the moment it reaches the first pre-existing duplicate pair — it would work on an
empty test database and fail the instant it touched real data. That is exactly the
failure test_c344_dm_key_migration.py constructs and asserts against.

THE FIX: a ROW_NUMBER()-gated backfill. Within each duplicate group — same
(chapter_id-or-NIL_UUID, sorted user-id pair) among kind='dm' conversations with
EXACTLY two members — only the EARLIEST row by (created_at, id) gets a non-null
dm_key. Every later duplicate in that group keeps dm_key NULL, so the unique index
never sees two rows with the same key. This is deliberately NOT a general
"consolidate duplicate DMs" migration: it does not move messages or members between
conversations, does not delete anything, and does not choose a "winning" conversation
for future traffic beyond which one the app will now start reusing going forward.
Reconciling pre-existing duplicate history is a larger, separate piece of work than
this card asks for.

KEY SHAPE, matching routers/messages.py's `_dm_key` exactly:
    f"{chapter_id or NIL_UUID}:{sorted(member_ids, key=str)[0]}:{sorted(member_ids, key=str)[1]}"
NIL_UUID (models/messaging.py) stands in for a NULL chapter_id because Postgres
treats NULLs as pairwise-distinct in a UNIQUE index — a plain nullable-column unique
constraint would silently fail to dedupe the (more common) chapterless-DM case.
sorted(..., key=str) and Postgres's default uuid ordering agree: the canonical
hyphenated-lowercase-hex text form of a UUID sorts identically to its raw byte value,
since every UUID's text form has hyphens at the same fixed positions.

ELIGIBILITY: only kind='dm' conversations with EXACTLY two conversation_members rows
are keyed. A group, or an already-malformed multi-recipient "dm" row (unvalidated by
the app today), gets no key and is left exactly as it is — matching the app's existing
looseness rather than silently tightening it in a schema migration.

Chains off 0036 (`alembic heads` confirmed a single head there at authoring time). Downgrade
drops the index, then the column.
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

# Must equal str(app.models.messaging.NIL_UUID) — uuid.UUID(int=0).
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.execute("ALTER TABLE conversations ADD COLUMN dm_key TEXT")

    op.execute(
        f"""
        WITH dm_pairs AS (
            SELECT
                c.id,
                c.created_at,
                COALESCE(c.chapter_id::text, '{_NIL_UUID}') AS chapter_key,
                (
                    SELECT array_agg(cm.user_id ORDER BY cm.user_id)
                    FROM conversation_members cm
                    WHERE cm.conversation_id = c.id
                ) AS member_ids
            FROM conversations c
            WHERE c.kind = 'dm'
        ),
        eligible AS (
            SELECT id, created_at, chapter_key, member_ids
            FROM dm_pairs
            WHERE array_length(member_ids, 1) = 2
        ),
        ranked AS (
            SELECT
                id,
                chapter_key,
                member_ids,
                ROW_NUMBER() OVER (
                    PARTITION BY chapter_key, member_ids
                    ORDER BY created_at ASC, id ASC
                ) AS rn
            FROM eligible
        )
        UPDATE conversations c
        SET dm_key = ranked.chapter_key || ':' || (ranked.member_ids[1])::text
            || ':' || (ranked.member_ids[2])::text
        FROM ranked
        WHERE c.id = ranked.id AND ranked.rn = 1
        """
    )

    op.execute(
        "CREATE UNIQUE INDEX idx_conversations_dm_key "
        "ON conversations (dm_key) "
        "WHERE dm_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_conversations_dm_key")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS dm_key")
