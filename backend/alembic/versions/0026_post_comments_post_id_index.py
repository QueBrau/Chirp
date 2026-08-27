"""Index post_comments on (post_id, created_at) for live rows (board card c208).

THE ARCHITECTURE REVIEW BLAMED THE WRONG THING, and this migration is the correction.
S4 said the feed's correlated scalar subqueries were the problem and proposed rewriting
them as a LATERAL join or a pre-aggregation. The query plan says otherwise: `loops=50`
means each subquery runs once per OUTPUT row, not once per candidate row, so fifty
cheap index probes per page is a perfectly good shape.

The actual fault is that there was no index to probe. post_comments carried exactly one
index - post_comments_pkey on (id) - so `WHERE post_id = ...` had no choice but to scan
the whole table, fifty times per feed page, and again on every post open through
list_comments.

MEASURED on realistic cardinality (5,000 posts, 100,000 comments, ~20 per post), one
feed page:

    BEFORE  Seq Scan on post_comments, loops=50      612.768 ms
    AFTER   Bitmap Index Scan, loops=50                2.603 ms

That is the whole fix. No query logic changes, which matters: rewriting to LATERAL
would have meant touching the c109 agreement between the comment COUNT and what
list_comments actually returns, and that subtlety is worth more than the microseconds
it would have bought on top of this.

WHY THE INDEX IS SHAPED THIS WAY. Partial on `deleted_at IS NULL` because every reader
filters it - the count in _post_counts_select and the list in list_comments both do - so
there is no reason to carry deleted rows in the index. `created_at` trails `post_id`
because list_comments orders by it, so one index serves the count and the comment list
rather than needing two.

CONCURRENTLY, IN AN AUTOCOMMIT BLOCK, AND THE TRADE IS REAL. A plain CREATE INDEX takes
a SHARE lock on post_comments and blocks every write to it until the build finishes -
unnoticeable on a small table, an outage on a large one, and this migration exists
precisely because that table is expected to get large. The cost of CONCURRENTLY is that
it cannot run inside a transaction (hence the autocommit block) and that a FAILED build
leaves an INVALID index behind, which does not serve queries and is not replaced by a
retry. If this migration fails, check for it before re-running:

    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
    DROP INDEX idx_post_comments_post_live;

IF NOT EXISTS is deliberately NOT used here for that exact reason: it would silently
skip an invalid leftover and leave the table unindexed while reporting success.

Number claimed as 0026 on c208 BEFORE this file was written, per CLAUDE.md. Chains off
0025, the verified head (`alembic heads`).
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_post_comments_post_live "
            "ON post_comments (post_id, created_at) "
            "WHERE deleted_at IS NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        # IF EXISTS here, unlike upgrade: a downgrade should succeed whether or not the
        # upgrade got as far as a valid index, and dropping an invalid one is exactly
        # what someone recovering from a failed CONCURRENTLY build needs this to do.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_post_comments_post_live")
