"""Per-user random seed behind daily-rotating chirp pseudonyms (board card c390).

WHY A STORED RANDOM SEED AND NOT A HASH OF THE USER ID. The pseudonym has to be
stable for one author within a day and unguessable by everyone else. Deriving it
as hash(user_id, campus_id, day) satisfies the first half and fails the second:
user ids are not secret to insiders. A chapter member can already list their own
chapter's roster, so they would hold the exact input set needed to compute every
chapter-mate's pseudonym for a given day and deanonymize the board wholesale -
the same authorship-oracle shape c342 closed on blocks. A random per-user seed is
not derivable from anything an attacker can enumerate.

Chosen over a server-side HMAC secret because a secret needs Secret Manager
provisioning (a human infra step) and has to be identical across every Cloud Run
instance to produce consistent names; a column is correct everywhere the moment
this migration runs, with nothing to configure.

The column is filled per row rather than by one shared default: a single default
evaluated once would hand EVERY user the same seed, which silently collapses all
pseudonyms into one name and would look like it worked.

Revision ID: 0038
Revises: 0037
"""

from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Three steps rather than one ADD COLUMN with a default. PostgreSQL does
    # evaluate a VOLATILE default per row on ADD COLUMN, but that behaviour is a
    # detail worth not depending on when the failure mode is every user sharing a
    # seed. The explicit UPDATE is unambiguous at any server version.
    op.add_column("users", sa.Column("pseudonym_seed", sa.Text(), nullable=True))
    op.execute("UPDATE users SET pseudonym_seed = gen_random_uuid()::text")
    op.alter_column("users", "pseudonym_seed", nullable=False)
    # gen_random_uuid() is a PG13 builtin (no pgcrypto) backed by a strong RNG, so
    # new rows get their own seed without any application code remembering to.
    op.execute(
        "ALTER TABLE users ALTER COLUMN pseudonym_seed "
        "SET DEFAULT gen_random_uuid()::text"
    )


def downgrade() -> None:
    op.drop_column("users", "pseudonym_seed")
