"""conversation_members must be indexed on user_id for active rows (board card c212).

WHAT THIS PINS AND WHY IT IS A SCHEMA TEST RATHER THAN A TIMING ONE. The primary key on
conversation_members is (conversation_id, user_id) - conversation_id leads, so its
btree cannot serve a lookup keyed on user_id alone. list_conversations
(routers/messages.py) builds a user's inbox with `WHERE user_id = :id AND
left_at IS NULL`, and before migration 0027 that predicate had no index to use, so it
sequential-scanned the whole table on every inbox load.

A timing assertion would be the obvious test and the wrong one here too: it would pass
on an empty CI database whether or not the index existed, and go flaky under load. What
can be asserted honestly is that the index is PRESENT and has the shape the query
needs - the same reasoning as test_post_comments_index.py (c208).

These tests fail against the pre-0027 schema, where conversation_members carries only
its primary key on (conversation_id, user_id).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def _indexes_on(url: str, table: str) -> dict[str, str]:
    """Every index on `table`, name -> its CREATE INDEX definition.

    Builds and disposes its OWN engine rather than borrowing app.db's shared factory,
    matching test_post_comments_index.py / test_role_terms_backfill.py. These tests
    take `migrated_db` without the `client` fixture, so the app engine may be bound to
    an event loop that is already closed by the time they run.
    """
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
                {"t": table},
            )
            return {row[0]: row[1] for row in result}
    finally:
        await engine.dispose()


async def test_conversation_members_is_indexed_on_user_id(migrated_db: str) -> None:
    """The headline: a lookup by user_id has an index to use.

    Asserts on the leading column rather than the exact index name being present in a
    list, so the test still means something if the index is later renamed or widened.
    The primary key's own index leads on conversation_id, not user_id, so it must not
    satisfy this assertion by accident.
    """
    indexes = await _indexes_on(migrated_db, "conversation_members")

    usable = [
        name
        for name, definition in indexes.items()
        if "(user_id)" in definition.replace(" ", "")
        or "(user_id," in definition.replace(" ", "")
    ]
    assert usable, (
        "conversation_members has no index leading on user_id — list_conversations "
        f"will sequential-scan the whole table. Indexes present: {sorted(indexes)}"
    )


async def test_the_index_is_partial_on_active_membership(migrated_db: str) -> None:
    """Partial on left_at IS NULL, because the one reader that filters by user_id
    (list_conversations) filters this way too.

    A full index would also stop the seq scan, so this is not asserting correctness -
    it is pinning a deliberate choice (c212) that a later 'simplification' to a plain
    index would silently undo, carrying departed memberships for no reader.
    """
    definition = (await _indexes_on(migrated_db, "conversation_members")).get(
        "idx_conversation_members_user_active"
    )
    assert definition is not None, "idx_conversation_members_user_active is missing"
    assert "WHERE (left_at IS NULL)" in definition, (
        f"expected a partial index on active membership, got: {definition}"
    )


async def test_the_primary_key_still_leads_on_conversation_id(migrated_db: str) -> None:
    """Confirms the PK is NOT usable for the user_id predicate on its own — the reason
    this migration exists rather than list_conversations already being fast.

    If this ever fails, the schema changed underneath c212's premise and the whole
    justification above needs re-checking.
    """
    indexes = await _indexes_on(migrated_db, "conversation_members")
    pk_definition = indexes.get("conversation_members_pkey")
    assert pk_definition is not None, "conversation_members has no primary key index"
    compact = pk_definition.replace(" ", "")
    assert "(conversation_id,user_id)" in compact, (
        f"expected the primary key to lead on conversation_id, got: {pk_definition}"
    )
