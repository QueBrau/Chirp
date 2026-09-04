"""The shared "who can this user reach" query for contact outside a shared chapter
(board card c243, extended to search by c322).

WHO IS A LEGITIMATE RECIPIENT. `conversations.chapter_id` is documented as "NULL for
cross-chapter DMs" (SPEC §3), so a caller with no chapter_id in play may legitimately
reach a Sigma Chi member's Delta Gamma contact — people in DIFFERENT chapters on the
SAME campus, which is the exact social graph the campus feed and Chirp already serve.
Cross-CHAPTER is the documented feature; cross-CAMPUS is not, and nothing in SPEC, the
schema, or the mobile app asks for it. So a user is reachable when either:

  1. they share an ACTIVE chapter membership with the caller — chapter content, and
     per Jose's Aug 16 ruling (core/campus_access.py) chapter membership stands on its
     own without an .edu; or
  2. they are on the caller's campus and the CALLER is currently campus-verified — the
     same gate campus-wide reach goes through everywhere else.

Anything else is refused. A NULL campus on either side is not a match: comparing
User.campus_id to caller.campus_id when caller.campus_id is itself NULL would make
every campus-less account mutually "reachable", which is the `campus_id is not None`
shortcut core/campus_access.py explicitly forbids — this module only ever adds the
campus branch when caller.campus_id is not None, and ordinary SQL NULL semantics take
care of a candidate whose own campus_id is NULL (`col = value` is never true against
NULL, on either side).

The TARGET is deliberately not required to be verified. Requiring it would make every
not-yet-verified account unreachable, which during onboarding week is most of them;
the abuse this rule stops needs a verified .edu on the CALLER's side, and that is where
the bar belongs.

ONE QUERY, TWO READERS. `app.routers.messages._require_reachable_off_chapter` is the
validator half — it raises unless a PROPOSED set of recipients is a subset of this.
`app.routers.messages.search_users` (`GET /users/search`, board c322) is the set half —
it browses this query directly, plus its own exclusions (self, ghosts, suspended,
blocked) that are about being a valid SEARCH RESULT rather than about reachability, so
they do not belong in this file. Do not write a second copy of the reachability test
anywhere else: a search narrower than this frustrates a DM the API would otherwise
allow; a search wider than this lists someone the validator will then refuse, which is
worse — a picker showing a person who turns into a 403 on submit.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Select, or_, select

from app import models
from app.core.campus_access import is_campus_verified


def reachable_off_chapter_ids(caller: models.User) -> Select[tuple[uuid.UUID]]:
    """SELECT of user ids `caller` may contact outside a shared-chapter conversation.

    A plain, unexecuted SELECT of `users.id` — wrap it in `User.id.in_(...)` to test
    specific candidates (the validator), or select against it directly to browse the
    whole set (search). Deliberately never restricted to a candidate list here: that
    restriction is the caller's job, so the identical query serves both "is X in this
    set" and "list this set" instead of becoming two queries that could drift apart.
    """
    active_chapter_mates = select(models.Membership.user_id).where(
        models.Membership.status == "active",
        models.Membership.chapter_id.in_(
            select(models.Membership.chapter_id).where(
                models.Membership.user_id == caller.id,
                models.Membership.status == "active",
            )
        ),
    )
    conditions = [models.User.id.in_(active_chapter_mates)]
    if caller.campus_id is not None and is_campus_verified(caller):
        conditions.append(models.User.campus_id == caller.campus_id)
    return select(models.User.id).where(or_(*conditions))
