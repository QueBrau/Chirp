"""Let a student with no chapter post to their campus (board card c71).

The contradiction this closes: the Aug 11 product decision is that Chirp is for ALL
students and greek chapters are an optional thing you join, but the only create-post
route was POST /chapters/{chapter_id}/posts behind an active-membership check, and
posts.chapter_id was NOT NULL. A student in no org could therefore not post at all,
on a campus feed built for them. c49 hid the compose button for those users as
containment; this is the fix.

Shape, and why this one:

- posts.campus_id is added NOT NULL and backfilled from the post's chapter. Every
  post now names its campus directly, so the campus feed filters a column instead of
  JOINing chapters — a join a chapter-less post would silently drop out of. It also
  pins the post to the campus it was made on. Deriving campus from the author instead
  would have been one less column and wrong: a transferring student would drag their
  old posts onto their new campus feed.

- posts.chapter_id becomes NULLABLE, meaning "no org authored this". On a campus post
  made from inside a chapter it stays populated as provenance, so that post keeps
  appearing in its own chapter's feed exactly as before.

- ck_posts_org_requires_chapter is the guard that makes the nullability safe. 'org'
  means "private to this chapter", so an org post with no chapter is not a looser
  permission, it is an unscopeable one: every org read path filters by chapter_id, and
  a NULL there matches nothing while still being a row that exists. The database
  refuses it rather than trusting every future writer to remember.

The org-privacy guarantee is untouched: the campus feed still selects on
audience = 'campus', so an 'org' post cannot surface there no matter what its
chapter_id or campus_id say.

NOTE FOR WHOEVER MERGES THIS: down_revision is 0010 because 0011 (c76) and 0012 (c69)
were claimed on the board but not yet written when this was authored. If either has
landed by merge time, re-point down_revision at the real head first — two files
sharing a down_revision is the multiple-heads version of the c41 collision.
"""
from alembic import op

revision = "0013"
# THE PARENT IS "WHATEVER MAIN'S HEAD IS AT THE MOMENT THIS MERGES", not a number
# anyone can write down in advance (board c101). This pointer has now gone stale
# three times: 0010 -> 0011 -> 0014 -> 0016. Each time main grew a migration while
# this branch sat, and each time the symptom was identical - two heads, and
# `alembic upgrade head` failing outright on the next deploy rather than picking
# one. Re-point, never renumber: 0013 is a validly claimed number.
#
# 0016 is main's head as of this merge (0011 -> 0014 -> 0017 -> 0015 -> 0016).
# If this branch sits again, check `alembic heads` on main before merging.
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE posts ADD COLUMN campus_id UUID REFERENCES campuses(id)")
    # Deterministic: chapter_id is still NOT NULL at this point and chapters.campus_id
    # is NOT NULL, so every existing row gets a campus and the SET NOT NULL below
    # cannot fail on legacy data.
    op.execute(
        """
        UPDATE posts
           SET campus_id = chapters.campus_id
          FROM chapters
         WHERE chapters.id = posts.chapter_id
        """
    )
    op.execute("ALTER TABLE posts ALTER COLUMN campus_id SET NOT NULL")
    op.execute("ALTER TABLE posts ALTER COLUMN chapter_id DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE posts
          ADD CONSTRAINT ck_posts_org_requires_chapter
          CHECK (audience = 'campus' OR chapter_id IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_posts_campus_time
            ON posts (campus_id, created_at DESC)
         WHERE deleted_at IS NULL AND audience = 'campus'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_posts_campus_time")
    op.execute("ALTER TABLE posts DROP CONSTRAINT IF EXISTS ck_posts_org_requires_chapter")
    # Chapter-less posts cannot survive a downgrade: restoring NOT NULL on chapter_id
    # has no value to put there, and inventing one would file a student's post under an
    # org they never joined. Downgrading means abandoning the feature, so the rows it
    # made possible go with it. Stated plainly because it is real data loss.
    op.execute("DELETE FROM posts WHERE chapter_id IS NULL")
    op.execute("ALTER TABLE posts ALTER COLUMN chapter_id SET NOT NULL")
    op.execute("ALTER TABLE posts DROP COLUMN campus_id")
