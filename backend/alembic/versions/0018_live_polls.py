"""Live polls for the secretary dashboard (board card c162).

Three tables. The interesting parts are the two constraints that make bad states
unrepresentable rather than merely discouraged:

1. `poll_votes` is keyed on (poll_id, user_id). One vote per member per poll is a
   PRIMARY KEY, not a rule the application remembers to enforce. Changing a vote
   is an UPDATE of that row. Two simultaneous ballots from the same member cannot
   both land, without the route needing a lock or a read-then-write.

2. `poll_votes.option_id` points at poll_options through a COMPOSITE foreign key
   on (option_id, poll_id), which is why `poll_options` carries a UNIQUE (id,
   poll_id) that looks redundant next to its primary key. It is not redundant --
   it is the target that composite key needs. Without it, voting in poll A with an
   option belonging to poll B is a perfectly valid row, and the resulting tally is
   wrong in a way no test would think to look for: every count is a plausible
   number, just not the right one.

PARENTED ON 0013, WHICH IS THE HEAD. Not on 0017, which is the highest filename
and is in the middle of the chain: 0011 -> 0014 -> 0017 -> 0015 -> 0016 -> 0013.
Alembic walks down_revision, not filenames. `alembic heads` was run and read 0013
before this file was written, per HANDOFF's migration rules, and 0018 was claimed
on the board first so a second branch cannot take the same number.

Ballot secrecy is a schema-level intent here too: nothing in the read path joins
poll_votes to users, and no index invites it. See models/polls.py.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "polls",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'open'"), nullable=False
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_polls"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], name="fk_polls_chapter"),
        # SET NULL, not CASCADE: deleting a meeting must not silently destroy the
        # record of how the chapter voted in it. The poll outlives its agenda item.
        sa.ForeignKeyConstraint(
            ["meeting_id"], ["meetings.id"], name="fk_polls_meeting", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_polls_created_by"),
        sa.CheckConstraint("status IN ('open','closed')", name="ck_polls_status"),
    )
    op.create_index("ix_polls_chapter_id", "polls", ["chapter_id"])
    op.create_index("ix_polls_meeting_id", "polls", ["meeting_id"])

    op.create_table(
        "poll_options",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("poll_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_poll_options"),
        sa.ForeignKeyConstraint(
            ["poll_id"], ["polls.id"], name="fk_poll_options_poll", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("poll_id", "position", name="uq_poll_options_poll_position"),
        # The target of poll_votes' composite FK. See the module docstring.
        sa.UniqueConstraint("id", "poll_id", name="uq_poll_options_id_poll"),
    )

    op.create_table(
        "poll_votes",
        sa.Column("poll_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("option_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("poll_id", "user_id", name="pk_poll_votes"),
        sa.ForeignKeyConstraint(
            ["poll_id"], ["polls.id"], name="fk_poll_votes_poll", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_poll_votes_user"),
        sa.ForeignKeyConstraint(
            ["option_id", "poll_id"],
            ["poll_options.id", "poll_options.poll_id"],
            name="fk_poll_votes_option_in_poll",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("poll_votes")
    op.drop_table("poll_options")
    op.drop_index("ix_polls_meeting_id", table_name="polls")
    op.drop_index("ix_polls_chapter_id", table_name="polls")
    op.drop_table("polls")
