"""Role constants and the require_role dependency factory."""
from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Any

from fastapi import Depends, HTTPException

from app import models
from app.core.errors import forbidden
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership


class Role(str, Enum):
    """Chapter roles, mirroring the memberships.role CHECK constraint."""

    president = "president"
    vice_president = "vice_president"
    treasurer = "treasurer"
    secretary = "secretary"
    historian = "historian"
    member = "member"
    pledge = "pledge"
    alumni = "alumni"


EBOARD: frozenset[Role] = frozenset(
    {Role.president, Role.vice_president, Role.treasurer, Role.secretary, Role.historian}
)

# ---- capabilities (board card c80) ----
#
# Each set is the SINGLE definition of who may do a thing. The routers gate on them
# (require_role(*DUES_ADMIN)) and GET /chapters/{id}/role-meta reports them, so the
# app never keeps its own copy of the taxonomy.
#
# It kept one anyway, which is why this exists: chapter/index.tsx hardcoded
# roles: ["treasurer","president"] and ["secretary","president"] for its dashboard
# tiles while the invite card in the same file read the server's answer. One half of
# one screen trusted the backend and the other half guessed. The guess was already
# wrong - vice_president and historian are in EBOARD and got no tools at all - and
# nothing failed when it drifted, because a client-side role list that disagrees with
# the server produces a 403 the user never sees, or a tile that was never drawn.
#
# Adding a capability means adding it HERE and using it at the gate. If a route gates
# on a literal role tuple instead, the UI cannot know about it and the drift starts
# over.
DUES_ADMIN: frozenset[Role] = frozenset({Role.treasurer, Role.president})
MINUTES_ADMIN: frozenset[Role] = frozenset({Role.secretary, Role.president})
# President only, deliberately: PATCH /chapters/{id}/members can change roles, which
# includes changing someone else's. See c83 - the person granting a role can be the
# person losing one, and there is exactly one president.
MEMBERS_ADMIN: frozenset[Role] = frozenset({Role.president})
# Same membership as MINUTES_ADMIN today, deliberately kept as its own name (c162).
# Running a vote and writing the minutes are different jobs that happen to share an
# officer; collapsing them into one constant would mean a chapter that later lets,
# say, the VP run polls has to either widen minutes editing too or unpick the two.
POLLS_ADMIN: frozenset[Role] = frozenset({Role.secretary, Role.president})

# READ-ONLY, deliberately not named *_admin: c163's product ruling made the Vice
# President dashboard a DEPUTY PRESIDENT view — roster, open invites, and a dues
# status summary, framed as a stand-in for the president — with delegation (acting
# on any of it) explicitly out of the alpha build. It is NOT members_admin: that set
# also gates PATCH /chapters/{id}/members and PATCH /chapters/{id}, and granting it
# would hand the VP mutation rights the ruling never asked for. President is included
# for the same reason every other capability includes them (see chapter_overview's
# docstring in routers/chapters.py) - they already hold the superset of this data via
# their own overview, so this is not a new door for them.
DEPUTY_OVERVIEW: frozenset[Role] = frozenset({Role.vice_president, Role.president})

CAPABILITIES: dict[str, frozenset[Role]] = {
    "dues_admin": DUES_ADMIN,
    "minutes_admin": MINUTES_ADMIN,
    "members_admin": MEMBERS_ADMIN,
    "polls_admin": POLLS_ADMIN,
    "moderation": EBOARD,
    # Lineage writes (families, big/little edges) — routers/lineage.py gates on
    # require_role(*EBOARD), the same set this maps to (c79). SPEC names the
    # historian for the job, but edit rights are "Historian/e-board" (§7 m6).
    "lineage_admin": EBOARD,
    "deputy_overview": DEPUTY_OVERVIEW,
}


def capabilities_for(role: str) -> list[str]:
    """Capability names this role holds, for the client to render against.

    Returned as names rather than role lists on purpose: the app should ask "may I
    see the dues tile" and never "am I a treasurer or a president".
    """
    return sorted(name for name, roles in CAPABILITIES.items() if role in {r.value for r in roles})


def require_role(*roles: Role) -> Callable[..., Coroutine[Any, Any, models.Membership]]:
    """Dependency factory layered on get_current_membership; 403 unless role is allowed."""
    allowed: frozenset[str] = frozenset(role.value for role in roles)

    async def _require_role(
        membership: models.Membership = Depends(get_current_membership),
    ) -> models.Membership:
        if membership.role not in allowed:
            raise HTTPException(status_code=403, detail="insufficient_role")
        return membership

    return _require_role


async def require_platform_admin(
    user: models.User = Depends(get_current_user),
) -> models.User:
    """Dependency gate for platform-level actions (chapter creation, account
    moderation): the same is_platform_admin check chapters.create_chapter already
    uses inline (SECURITY-REVIEW finding 1 / board card c28), promoted to a shared
    dependency so board card c76's suspend/unsuspend routes don't re-derive it.

    There is no "platform" row to hold a role the way require_role() looks one up on
    a Membership, so this checks the flag directly rather than inventing one.
    """
    if not user.is_platform_admin:
        raise forbidden("platform_admin_required")
    return user
