""".edu campus verification: the column the campus gate actually keys on (c86 + c88).

THE HOLE THIS CLOSES, stated plainly because the schema alone does not show it. Until
now the only authorization check on campus-wide content anywhere in the backend was the
literal comparison `user.campus_id != campus_id -> 403`, in routers/feed.py and
routers/yaks.py. There was no verification concept in the schema at all, so there was
nothing else a gate could have consulted.

That was survivable while nothing wrote users.campus_id. c96 changed it: redeeming a
chapter invite now writes campus_id from the chapter. Combined with c105 — a chapter
invite is an unlimited-use bearer token with optional expiry and no revocation — one
forwarded code became enough to read AND write a campus's Yak board and campus feed
with no email of any kind. Jose's c88 ruling is the opposite: chapter membership grants
CHAPTER content only, and the campus feed and Yak both require a real .edu.

So this migration adds the thing the gate keys on. The distinction that matters:
`campus_id` answers "which campus is this user associated with" and is reachable
without email. `campus_verified_at` answers "when did this user last prove an address
at that campus" and is not. Every campus-wide check moves to the second question.

THREE CHANGES:

1. users.campus_verified_at — NULL means never verified. Every existing user starts
   NULL, which is correct and is the point: nobody on prod has ever proved an .edu, so
   nobody should be grandfathered past a gate that did not exist when they signed up.
   This deliberately DARKENS the campus feed and Yak for every current user, including
   the ones c96's backfill just gave a campus_id to. That is the ruling working, not a
   regression.

2. campuses.email_domains — the addresses that prove attendance, lowercase, no '@'.
   NULL/empty means the campus cannot be verified at all, and it fails CLOSED: no
   domain configured means no code is ever sent, rather than any address passing.

3. campus_verifications — one row per code sent, with the code stored only as a salted
   hash, plus expiry, single-use consumption and an attempt counter.

DOWN reverses all three cleanly, and unlike 0014 it is safe to do so: dropping a
verification is not the same class of act as destroying a campus association, because
re-verifying is a thing a user can simply do again. Note the consequence anyway — a
downgrade re-opens the bypass, since the gate has nothing left to read.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0017"
branch_labels = None
depends_on = None

# THE PARENT IS DELIBERATELY NOT 0014, AND DELIBERATELY OUT OF NUMERIC ORDER.
#
# This file was written parented on 0014 while it was the head. c91's 0017 then merged
# to main with the SAME parent, which is two migrations claiming 0014 as their base —
# and `alembic upgrade head` does not pick one, it fails outright with multiple heads.
#
# Re-pointed rather than renumbered, following the rule this repo already set when
# c71's 0013 hit the same collision: the side that has NOT merged re-points at the
# current head, because renumbering a revision id breaks anything that already recorded
# it. Numeric order is cosmetic; the down_revision chain is what alembic actually walks.
#
# If you land another migration after this one, take main's real head at that moment
# rather than the highest number on disk — they are not the same thing any more.


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("campus_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "campuses",
        sa.Column("email_domains", postgresql.ARRAY(sa.Text()), nullable=True),
    )

    op.create_table(
        "campus_verifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "campus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campuses.id"),
            nullable=False,
        ),
        sa.Column("edu_email", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # The hot path is "newest unconsumed code for this user", checked on every submit.
    op.create_index(
        "ix_campus_verifications_user_pending",
        "campus_verifications",
        ["user_id", "sent_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_campus_verifications_user_pending", table_name="campus_verifications"
    )
    op.drop_table("campus_verifications")
    op.drop_column("campuses", "email_domains")
    op.drop_column("users", "campus_verified_at")
