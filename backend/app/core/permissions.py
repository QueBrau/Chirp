"""Role constants and the require_role dependency factory."""
from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Any

from fastapi import Depends, HTTPException

from app import models
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
