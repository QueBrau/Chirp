"""post_comments must be indexed on (post_id) for live rows (board card c208).

WHAT THIS PINS AND WHY IT IS A SCHEMA TEST RATHER THAN A TIMING ONE. The fault c208
fixes is not a wrong answer - every feed and comment test passed with the table
unindexed, and still does. It is that `WHERE post_id = ...` had no index to use, so the
feed's comment count seq-scanned the whole comments table fifty times per page
(measured: 612ms -> 2.6ms on 5k posts / 100k comments).

A timing assertion would be the obvious test and the wrong one: it would pass on an
empty CI database whether or not the index existed, and would go flaky the moment CI
got busy. What can be asserted honestly is that the index is PRESENT and has the shape
the queries need - anything more is measurement, and measurement belongs in the card
where the numbers are recorded with the conditions that produced them.

These tests fail against the pre-0026 schema, where post_comments carries only its
primary key on (id).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def _indexes_on(url: str, table: str) -> dict[str, str]:
    """Every index on `table`, name -> its CREATE INDEX definition.

    Builds and disposes its OWN engine rather than borrowing app.db's shared factory,
    matching test_role_terms_backfill.py. These tests take `migrated_db` without the
    `client` fixture, so the app engine may be bound to an event loop that is already
    closed by the time they run - which surfaces as `RuntimeError: Event loop is closed`
    masquerading as an assertion failure.
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


async def test_post_comments_is_indexed_on_post_id(migrated_db: str) -> None:
    """The headline: a lookup by post_id has an index to use.

    Asserts on the leading column rather than the exact index name being present in a
    list, so the test still means something if the index is later renamed or widened.
    """
    indexes = await _indexes_on(migrated_db, "post_comments")

    usable = [
        name
        for name, definition in indexes.items()
        if "(post_id" in definition.replace(" ", "").replace("USINGbtree", "")
        or "(post_id," in definition.replace(" ", "")
    ]
    assert usable, (
        "post_comments has no index leading on post_id — the feed's comment count and "
        f"list_comments will both sequential-scan the whole table. Indexes present: "
        f"{sorted(indexes)}"
    )


async def test_the_index_is_partial_on_live_rows(migrated_db: str) -> None:
    """Partial on deleted_at IS NULL, because every reader filters that way.

    A full index would also stop the seq scan, so this is not asserting correctness - it
    is pinning a deliberate choice (c208) that a later 'simplification' to a plain index
    would silently undo, carrying deleted rows for no reader.
    """
    definition = (await _indexes_on(migrated_db, "post_comments")).get(
        "idx_post_comments_post_live"
    )
    assert definition is not None, "idx_post_comments_post_live is missing"
    assert "WHERE (deleted_at IS NULL)" in definition, (
        f"expected a partial index on live rows, got: {definition}"
    )


async def test_the_index_also_covers_the_comment_list_ordering(migrated_db: str) -> None:
    """created_at trails post_id so one index serves BOTH readers.

    list_comments filters on post_id and orders by created_at. Dropping created_at from
    this index would leave that endpoint sorting on every call, which is the reason the
    column is there and not an accident of copy-paste.
    """
    definition = (await _indexes_on(migrated_db, "post_comments")).get(
        "idx_post_comments_post_live"
    )
    assert definition is not None, "idx_post_comments_post_live is missing"
    compact = definition.replace(" ", "")
    assert "(post_id,created_at)" in compact, (
        f"expected the index to lead on post_id and carry created_at, got: {definition}"
    )
