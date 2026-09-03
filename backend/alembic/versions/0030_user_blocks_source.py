"""Record WHY a block was created, so anonymity survives it (board card c279).

THE LEAK THIS CLOSES. POST /moderation/blocks/by-chirp/{chirp_id} lets someone block
an anonymous chirp's author without ever learning who they are, and that endpoint goes
to real lengths to keep it that way - no response body, no 409 split, an unconditional
upsert so even the LATENCY is constant. But it wrote an ORDINARY user_blocks row,
indistinguishable from a manual block, and that row filters the blocker's NAMED
surfaces too. So: snapshot the feed, block by chirp, re-fetch. The named person who
vanished IS the anonymous author, by display_name and author_id both. Then DELETE the
block and the feed comes back, with nothing durable marking that it happened.

The endpoint was airtight; the ROW it wrote was the leak.

WHAT THE COLUMN BUYS. With provenance recorded, the read filters can tell the two
apart: named blocks hide everything exactly as today, while by-chirp blocks hide only
chirp surfaces. The named feed then STOPS MOVING when a by-chirp block lands, and the
before/after diff that named the author has nothing to show.

Contact enforcement (app/core/blocks.py) deliberately keeps enforcing BOTH kinds - see
that module's docstring. This is the migration it recorded as deferred.

DEFAULT 'named' IS A FORWARD GUARD, NOT A DATA DECISION. The manager read prod when
c237 shipped and user_blocks held zero rows; it is still empty. So nothing is being
back-classified here. The default exists because the house order is migrate first, then
deploy: between this migration and the redeploy, the still-old code inserts rows with no
source, and 'named' is the safe value for those - it preserves today's hide-everything
behavior rather than silently un-hiding content in the window. Fail toward more
protection, never less.

THE CHECK CONSTRAINT is deliberate rather than a bare TEXT column. A typo'd source
('by-chirp' with a hyphen, say) would not error anywhere - it would simply never match
'named' in the read filters, and the block would quietly stop hiding named content. That
is the failure this whole card is about, arriving through a spelling mistake.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_ALLOWED = "source IN ('named', 'by_chirp')"


def upgrade() -> None:
    # server_default, not a plain default: existing rows need a value in the same
    # statement, and the old code still running during the deploy window needs one too.
    op.add_column(
        "user_blocks",
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'named'"),
        ),
    )
    op.create_check_constraint("ck_user_blocks_source", "user_blocks", _ALLOWED)


def downgrade() -> None:
    # Dropping the column loses provenance, which means every surviving block reverts to
    # hiding everything - the pre-c279 behavior, and the safe direction to fail in.
    op.drop_constraint("ck_user_blocks_source", "user_blocks", type_="check")
    op.drop_column("user_blocks", "source")
