"""Rename Yak to Chirps: tables, and the moderation target_type value (board card c179).

braul's call, Aug 25, taken after the four possible depths (copy / client / routes / DB)
and their risks were put to him. This is the deepest one.

THIS FILE MUST KEEP THE OLD NAMES ON THE LEFT-HAND SIDE, and that is not a style note:
the project-wide find-and-replace that did the rename swept this file too on the first
attempt and produced `rename_table("chirps", "chirps")`, which failed instantly against
a real database. A migration is the one place where BOTH names have to coexist - it is
the hinge between them. The same reasoning is why 0001 still says `yaks` and was excluded
from the rename entirely: it is a record of what actually ran, and every database that
has not yet reached 0022 still has a table by that name.

TWO KINDS OF CHANGE LIVE HERE, and the second is the one that needs care.

1. RENAMES OF SCHEMA OBJECTS. `yaks` -> `chirps`, `yak_votes` -> `chirp_votes`, and the
   COLUMN `yak_votes.yak_id` -> `chirp_id`. Renaming a table does NOT rename its columns,
   its indexes or its constraints - Postgres carries them through untouched under their
   old names. The column rename is load-bearing (the ORM reads `chirp_id` and the suite
   fails without it); the index and constraint names are cosmetic but renamed too, since
   "everywhere" was the instruction and a `yaks_pkey` sitting on a table called `chirps`
   is exactly the kind of residue that confuses the next person reading a catalog.

2. A DATA MIGRATION, ON TWO TABLES. Both `content_reports.target_type` AND
   `moderation_actions.target_type` store the literal 'yak' behind a CHECK constraint,
   and prod already has rows holding that value in each. Every one has to become 'chirp',
   with the constraint widened and narrowed around the rewrite.

   The second table was MISSED on the first pass, because content_reports was found by
   reading the model and moderation_actions was not. What caught it was the suite, not a
   review. The lesson is in the code below: the tables are enumerated from a query over
   pg_constraint for definitions mentioning the old value, rather than from whichever
   models a person happened to open.

THE ORDER IS THE WHOLE CORRECTNESS ARGUMENT. Drop the constraint first, THEN rewrite the
rows, THEN add the constraint back naming only the new value. Adding the new constraint
before the backfill fails immediately on every existing 'yak' row, and updating the rows
with the old constraint still in place fails just as hard.

THE CONSTRAINT'S REAL NAME IS NOT THE ONE THE MODEL DECLARES, verified against a live
Postgres catalog rather than read off the ORM. models declared `ck_content_reports_target_type`;
every database that ran 0001 actually has `content_reports_target_type_check`, Postgres's
auto-generated name for an unnamed inline column CHECK. That declared name was never
realized anywhere. 0019 hit precisely this on posts.audience and wrote it down - this
migration drops by the REAL name and recreates under the DECLARED one, closing the drift.

REVERSIBLE, and the downgrade is a true mirror: the same three steps in the same order
with the values swapped, so a rollback cannot strand rows holding a value its constraint
forbids. Proven by upgrade -> downgrade -> upgrade against a database carrying a real
'yak' report row.

DEPLOY IS NOT THE USUAL ONE. Routes move in the same PR as the tables, so prod must be
migrated AND redeployed in one window: the new code cannot read `yaks`, and the old code
cannot read `chirps`. Between the two steps the live backend errors. That gap is
unavoidable with a table rename and belongs at a quiet hour - it is recorded on c179.

Number claimed as 0022 on c179 BEFORE this file was written. CHAINS OFF 0021, not 0020:
0021 (c83's role_terms) landed on main while this branch was being cut, and `alembic
heads` is what caught it - chaining off 0020 as first written produced TWO heads, exactly
the hazard the single-head CI gate exists for. Re-read `alembic heads` after the last
pull, never before it.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Indexes and primary keys, which a table rename does not touch. IF EXISTS on the index
# renames because a pkey index is renamed by the CONSTRAINT rename below, not here.
_OBJECT_RENAMES = [
    ("idx_yaks_campus_time", "idx_chirps_campus_time"),
]
_CONSTRAINT_RENAMES = [
    ("chirps", "yaks_pkey", "chirps_pkey"),
    ("chirps", "yaks_campus_id_fkey", "chirps_campus_id_fkey"),
    ("chirps", "yaks_author_id_fkey", "chirps_author_id_fkey"),
    ("chirp_votes", "yak_votes_pkey", "chirp_votes_pkey"),
    ("chirp_votes", "yak_votes_value_check", "chirp_votes_value_check"),
    ("chirp_votes", "yak_votes_yak_id_fkey", "chirp_votes_chirp_id_fkey"),
    ("chirp_votes", "yak_votes_user_id_fkey", "chirp_votes_user_id_fkey"),
]

# (table, real existing constraint name, name to recreate under, old values, new values).
# The value LISTS differ per table and are not interchangeable - content_reports allows
# message_forward, moderation_actions allows report. Sharing one list between them would
# silently widen both.
_TARGET_TYPE_TABLES = [
    (
        "content_reports",
        "content_reports_target_type_check",
        "ck_content_reports_target_type",
        "'yak','post','comment','message_forward','user'",
        "'chirp','post','comment','message_forward','user'",
    ),
    (
        "moderation_actions",
        "moderation_actions_target_type_check",
        "ck_moderation_actions_target_type",
        "'user','yak','post','comment','report'",
        "'user','chirp','post','comment','report'",
    ),
]


def upgrade() -> None:
    op.rename_table("yaks", "chirps")
    op.rename_table("yak_votes", "chirp_votes")
    # A table rename leaves its COLUMNS alone. The ORM reads chirp_id.
    op.alter_column("chirp_votes", "yak_id", new_column_name="chirp_id")
    for old, new in _OBJECT_RENAMES:
        op.execute(f'ALTER INDEX IF EXISTS {old} RENAME TO {new}')
    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}')

    # Drop by the name the database actually has, not the one the model claimed.
    for table, old_name, new_name, _old_values, new_values in _TARGET_TYPE_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {old_name}")
        op.execute(f"UPDATE {table} SET target_type = 'chirp' WHERE target_type = 'yak'")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {new_name} "
            f"CHECK (target_type IN ({new_values}))"
        )


def downgrade() -> None:
    for table, old_name, new_name, old_values, _new_values in _TARGET_TYPE_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {new_name}")
        op.execute(f"UPDATE {table} SET target_type = 'yak' WHERE target_type = 'chirp'")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {old_name} "
            f"CHECK (target_type IN ({old_values}))"
        )

    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}')
    for old, new in _OBJECT_RENAMES:
        op.execute(f'ALTER INDEX IF EXISTS {new} RENAME TO {old}')
    op.alter_column("chirp_votes", "chirp_id", new_column_name="yak_id")
    op.rename_table("chirp_votes", "yak_votes")
    op.rename_table("chirps", "yaks")
