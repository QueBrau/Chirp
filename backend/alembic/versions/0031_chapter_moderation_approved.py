"""Decouple campus moderation from "e-board anywhere" (board card c308).

THE VECTOR THIS CLOSES, and it is not open yet - this lands FIRST, on purpose. braul
asked for self-serve org creation. POST /chapters already exists and already makes its
creator a president; it is gated on is_platform_admin (c28 / SECURITY-REVIEW Aug 13,
"closes the mechanism finding 1 depended on"). The reason that gate cannot simply be
removed is moderation.py: GET /moderation/reports is scoped to campuses where the
caller is active e-board, and reports carry forwarded_plaintext of reported E2EE
messages. Founding a chapter makes you e-board on that chapter's campus, so ungating
creation without this migration would let ANY user mint a throwaway chapter on ANY
campus and read that campus's moderation queue. That is finding 1, reachable again.

The sequencing is load-bearing: this is verified before creation is ungated, or there
is a window where anyone can self-appoint AND the privilege is still attached.

WHY THE KEY IS ON THE CHAPTER, NOT ON THE PERSON. braul's ruling (Sep 4) is "keep
moderation access on eboard only" - every sitting officer keeps exactly what they have
today, nobody is downgraded, so nothing needs announcing. An explicit per-USER grant
would satisfy that on the day it ships and then decay: backfill every current officer,
and next semester's newly elected treasurer of a long-established chapter has no grant
and silently loses the queue their predecessor had. The ruling is a rule about ROLES,
not a snapshot of people. Keying on the chapter keeps officer turnover working exactly
as it does now, while a self-made chapter confers nothing because its row is FALSE.

THE BACKFILL IS LIKE-FOR-LIKE, and the column order below is what makes that true
without opening a window. The house order is migrate first, then deploy, so between
this migration and the redeploy the OLD code is still creating chapters and knows
nothing about this column. Adding the column with server_default FALSE and only THEN
updating existing rows to TRUE means:

  - every chapter that existed when this ran is approved -> every current e-board
    member's access is byte-for-byte what it was;
  - every chapter created from here on, including during the deploy window and
    including by a platform admin, starts unapproved.

Doing it the other way round - default TRUE, then alter the default to FALSE - would
approve anything created between the two statements. Fail toward less privilege.

WHAT IS DELIBERATELY NOT HERE. Nothing can SET this column except direct database
access, exactly as is_platform_admin has worked since c28. That is the existing
precedent rather than a new gap, but an approval API should exist before self-serve
creation ships, or approving a real org means a psql session. Carded separately.

Chapter-level power is untouched by this and does not live in moderation.py at all:
feed.py's DELETE /chapters/{id}/posts/{id} is author-or-president gated on membership
alone, so a president of an unapproved chapter still administers their own chapter's
content in full. What an unapproved chapter loses is campus reach, which is the entire
point of the card.
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default FALSE, not a plain default: existing rows need a value in the same
    # statement, and the old code still running during the deploy window needs one too.
    op.add_column(
        "chapters",
        sa.Column(
            "moderation_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # The backfill, AFTER the column exists with the safe default. Every chapter that
    # existed at this moment is approved; the default stays FALSE for everything after.
    op.execute("UPDATE chapters SET moderation_approved = true")


def downgrade() -> None:
    op.drop_column("chapters", "moderation_approved")
