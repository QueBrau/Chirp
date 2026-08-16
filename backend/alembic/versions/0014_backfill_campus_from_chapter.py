"""Backfill users.campus_id from the chapter they already belong to (board card c96).

No schema change — this is a DATA migration, and it exists because of a gap nobody
noticed for days. c85 correctly stopped trusting the client to assert a campus at
bootstrap and named c86's `.edu` redemption as the only writer of users.campus_id.
c86 was then deferred until after alpha on the reasoning that chapter membership
grants org content with no email needed. That reasoning is true about ORG content and
was verified against prod — Orgs, events, members, dues and invites all work perfectly
with campus_id NULL.

What it missed is that campus_id then had NO writer at all. Every user on prod sat at
NULL permanently, which dead-ends Home's Campus tab, refuses the entire Yak tab
("Yak is tied to a campus account"), and makes alpha gate c71 — "a campus-only student
can post" — unreachable by construction, since feed.py gates campus posts on
`user.campus_id == campus_id`.

The routers now derive campus from the chapter on join and on chapter creation
(app/routers/chapters.py). That fixes everyone who joins from here on. This migration
fixes everyone who already did — on prod today that is the QA accounts, but it is
written for the general case because a real chapter may have joined before this lands.

WHY THIS IS NOT A ROLLBACK OF c85: the value is read off chapters.campus_id, which is
set when a PLATFORM ADMIN creates the chapter. It never originates from a request body.
A user's only input is an invite code that an e-board member deliberately minted for
them, which is a human vouching — the same kind of evidence .edu verification collects,
arriving earlier in the funnel.

ONLY FILLS NULLS, in both directions of thinking:
- It will not overwrite a campus a user already has, so it cannot clobber anything c86
  writes later. When c86 ships, a verified .edu is the stronger claim and must win.
- Users with no membership stay NULL, which is the correct state for someone who has
  proved nothing. They keep working exactly as they do today: org content when they
  join, campus content when they verify.

Ambiguity note: a user in more than one chapter could in principle span two campuses.
Chirp is single-org today (memberships[0] is treated as "the" chapter across the app),
and picking the earliest membership is both deterministic and the one they proved
first. If multi-campus users ever become real, campus stops being a scalar on users
and this becomes a different problem.

DOWN: deliberately a no-op, not an UPDATE ... SET campus_id = NULL. This migration
cannot distinguish a campus IT wrote from one written by a later c86 verification or by
the routers during normal use, so a literal reversal would silently destroy real,
user-proved data to undo a backfill. Losing correct data is worse than leaving a
correct value in place.
"""

from alembic import op

revision = "0014"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earliest ACTIVE membership wins. Active-only because a removed or inactive
    # membership is not a claim to a campus any more; earliest because it is
    # deterministic and it is the campus the user proved first. Postgres
    # DISTINCT ON gives one row per user without a window function or a
    # correlated subquery per row. Ordering is on joined_at (memberships has no
    # created_at) with id as the tie-break.
    op.execute(
        """
        UPDATE users
        SET campus_id = first_chapter.campus_id
        FROM (
            SELECT DISTINCT ON (m.user_id)
                   m.user_id,
                   c.campus_id
            FROM memberships m
            JOIN chapters c ON c.id = m.chapter_id
            WHERE m.status = 'active'
            ORDER BY m.user_id, m.joined_at ASC, m.id ASC
        ) AS first_chapter
        WHERE users.id = first_chapter.user_id
          AND users.campus_id IS NULL
        """
    )


def downgrade() -> None:
    # Intentionally empty — see the module docstring. Nulling campus_id here
    # would destroy campuses this migration never wrote.
    pass
