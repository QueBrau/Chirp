"""Invite codes stop being unlimited-use bearer tokens (board card c105).

WHAT WAS TRUE BEFORE THIS. `chapter_invites` carried code, role, a NULLABLE
expires_at and created_by, and nothing else. There was no use count, no cap, and
no revocation, and `join_chapter` checked only that the row existed and had not
expired. So a code minted with no expiry — the default the mint endpoint offered —
was a bearer token that worked forever, for anyone holding the string, with no way
to kill it short of a hand-written DELETE against prod.

WHY IT MATTERS MORE NOW THAN IT DID LAST WEEK. c96 made invite redemption write
`users.campus_id`, and per c104 the only authorization check on campus content
today is a literal `user.campus_id != campus_id` comparison. So the same forwarded
string that used to buy one chapter's org content now also mints campus identity,
which is what unlocks Yak and the campus feed. The token did not change; what it
is worth did. This migration is worth applying either way — an unrevocable,
never-expiring, unlimited-use credential is a defect on its own terms — but it is
what keeps c105 from compounding c104.

THREE COLUMNS AND A BACKFILL:

  max_uses    how many redemptions the code is good for. NOT NULL, so there is no
              representable "unlimited" any more. Default 25 because a chapter
              invite is genuinely a pledge-class artifact — a president posts one
              code for a room of people, and single-use would break the actual
              workflow rather than secure it. Capped at 200 by a CHECK so the
              cap cannot be argued back up to infinity one request at a time.
  uses        redemptions so far. The router increments it with a conditional
              UPDATE ... WHERE uses < max_uses rather than a read-modify-write,
              so two people redeeming the last seat at once cannot both win.
  revoked_at  set by the new revoke route; a president can kill a leaked code
              without a DB edit.

expires_at BECOMES NOT NULL, and that is the load-bearing half. A server-side
default at mint would leave every existing row untouched and would be one code
change away from being optional again. Making the column NOT NULL means no row in
this table can be never-expiring, whatever a future caller does. Existing rows
without an expiry are backfilled to seven days out rather than expired on the
spot: the QA accounts' codes are live and revoking them silently in a migration
would look exactly like a bug on the next walk-through.

Revision ID: 0016
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chapter_invites",
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default=sa.text("25")),
    )
    op.add_column(
        "chapter_invites",
        sa.Column("uses", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "chapter_invites",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_chapter_invites_max_uses_range",
        "chapter_invites",
        "max_uses >= 1 AND max_uses <= 200",
    )
    op.create_check_constraint(
        "ck_chapter_invites_uses_nonneg",
        "chapter_invites",
        "uses >= 0",
    )

    # Backfill BEFORE the NOT NULL, or the alter fails on every legacy row.
    op.execute(
        "UPDATE chapter_invites "
        "SET expires_at = now() + interval '7 days' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column(
        "chapter_invites",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "chapter_invites",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.drop_constraint("ck_chapter_invites_uses_nonneg", "chapter_invites")
    op.drop_constraint("ck_chapter_invites_max_uses_range", "chapter_invites")
    op.drop_column("chapter_invites", "revoked_at")
    op.drop_column("chapter_invites", "uses")
    op.drop_column("chapter_invites", "max_uses")
