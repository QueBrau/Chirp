"""Touse/Bouse weekly house leaderboard: one ballot per student per week (board card c175).

Every campus-verified student casts ONE ballot a week naming a top house (Touse) and,
optionally, a bottom house (Bouse). The weekly leaderboard and the semester title
("Touse of Fall 26") are both DERIVED from these rows at read time - nothing here
stores a standing.

STORING BALLOTS RATHER THAN STANDINGS IS THE LOAD-BEARING CHOICE. The product decision
to publish the bottom of the ranking publicly (braul, Aug 24, recorded on c175 with the
risk stated) is the single most likely thing to be walked back - by review, by an App
Store reviewer under Guideline 1.2, or by a chapter complaining. Because the raw ballots
are what is persisted, retreating to "Bouse visible only to that chapter's e-board" or
"name only the top half" is a SERIALIZER change needing no migration and no data
rewrite. A precomputed standings table would have turned a reversible decision into an
irreversible one.

ONE VOTE PER WEEK IS THE PRIMARY KEY, not application logic: (campus_id, week_start,
voter_id). A check-then-insert races under concurrency, and a UNIQUE index bolted on
later would have to cope with the duplicates it was added to prevent. yak_votes uses the
same trick for one-vote-per-yak; this is that pattern with a week in it.

WEEK_START IS A DATE THE SERVER COMPUTES. It is never accepted from a client - a
client-supplied week lets anyone stuff a past week after seeing how it went, which is
the cheapest possible attack on a weekly contest and would leave no trace in the data.

THE COMPOSITE FOREIGN KEY IS NOT DECORATION. (touse_chapter_id, campus_id) references
chapters(id, campus_id), so a ballot at one campus CANNOT name a chapter at another -
structurally, not by a validation somebody can forget. This repo has already shipped two
cross-campus leaks (c82's dual-chapter attendance join; SECURITY-REVIEW finding 1, where
moderation returned every report platform-wide) and BOTH passed every single-campus test
that existed at the time. A composite FK cannot be forgotten in a code path.

It needs UNIQUE (id, campus_id) on chapters as its target. `id` is already the primary
key, so that constraint adds no real restriction - Postgres simply requires a unique
constraint over exactly the referenced columns before it will accept the reference.

MATCH SIMPLE (the default) is what makes the optional Bouse work: a composite FK with
any NULL column is satisfied without a lookup, so (NULL, campus_id) passes and a
Touse-only ballot is legal. That is deliberate - forcing every voter to name a bottom
house would manufacture Bouse signal from people who did not want to cast one.

NO SEPARATE WEEK INDEX. The primary key is (campus_id, week_start, voter_id) and every
read filters on the leading two columns, so the PK's own index already serves them; a
second index over that prefix would be write cost for nothing.

Number claimed as 0020 on c175 BEFORE this file was written, per CLAUDE.md. Chains off
0019, the verified head (`alembic heads`). Note that file numbers here do not track
chain order - 0018 belongs to the unmerged q/c162-live-polls branch.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_chapters_id_campus", "chapters", ["id", "campus_id"]
    )
    op.create_table(
        "house_ballots",
        sa.Column("campus_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("voter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("touse_chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bouse_chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "campus_id", "week_start", "voter_id", name="pk_house_ballots"
        ),
        sa.ForeignKeyConstraint(["campus_id"], ["campuses.id"]),
        sa.ForeignKeyConstraint(["voter_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["touse_chapter_id", "campus_id"],
            ["chapters.id", "chapters.campus_id"],
            name="fk_house_ballots_touse_same_campus",
        ),
        sa.ForeignKeyConstraint(
            ["bouse_chapter_id", "campus_id"],
            ["chapters.id", "chapters.campus_id"],
            name="fk_house_ballots_bouse_same_campus",
        ),
        # A ballot naming the same house best AND worst is not a strong opinion, it is
        # a bug or an attempt to cancel a rival's vote out. Rejected in the schema so
        # no route can create one.
        sa.CheckConstraint(
            "bouse_chapter_id IS NULL OR bouse_chapter_id <> touse_chapter_id",
            name="ck_house_ballots_distinct_houses",
        ),
    )


def downgrade() -> None:
    op.drop_table("house_ballots")
    op.drop_constraint("uq_chapters_id_campus", "chapters", type_="unique")
