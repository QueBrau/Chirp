"""UserOut (wrapped by MeOut) carries the caller's own suspension state (board c126).

Safe to add here specifically: UserOut is used ONLY in self-facing responses
(POST /auth/bootstrap, GET /auth/me) — never a view of another user, so this can
never leak a stranger's moderation history. Naming matches the platform-admin
view in schemas/moderation.py.

A REAL DISCOVERY WHILE BUILDING THIS, worth stating plainly because it changes
what this field means: GET /auth/me does NOT go through
middleware/auth.py's get_current_user (the dependency c76 added the suspension
403 to). Its own docstring explains why, predating c76 entirely — it resolves
the user directly so an authenticated-but-unregistered caller gets 404, not
get_current_user's 401. Nobody revisited that when c76 landed, so /auth/me was
NEVER suspension-gated: a suspended account can call it today and get a full
200 with their real profile and memberships, while every other authenticated
route 403s them. That was found empirically, not assumed — see the first
attempt at this test file, which wrongly expected a 403 and failed with a 200
carrying the field.

Whether that's a bug or the intended design is a real, separate question this
ticket did not ask me to answer, so it is reported rather than silently fixed
either way: it MIGHT be deliberate (the one route a suspended user's client can
still call, specifically so it can render "you are suspended, here is why"
instead of a wall of 403s) or it might be a c76 gap nobody has hit yet because
nothing previously depended on reading your own state while suspended. Either
way, this field's addition is what makes the gap OBSERVABLE for the first time
— UserOut had no suspension field to see before now.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeUser


async def _grant_platform_admin(user_id: str) -> None:
    """Mirrors test_moderation_suspension.py's private helper (not imported —
    module-private there); suspending needs a platform admin and there is no API
    that grants the flag by design (board c28)."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


async def test_me_reports_null_when_never_suspended(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user("Ordinary Member")
    response = await client.get("/auth/me", headers=user.headers)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["suspended_at"] is None


async def test_me_still_200s_and_now_reports_suspended_at(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The real, verified behaviour: GET /auth/me is not suspension-gated (see
    module docstring), so it 200s for a suspended caller exactly as it did
    before this ticket — the only change is that the body can now say so."""
    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)
    target = await make_user("Rule Breaker")

    suspend = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "harassment"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text

    me = await client.get("/auth/me", headers=target.headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["suspended_at"] is not None

    # A different authenticated route DOES still 403 this same caller — c76's
    # enforcement is real, it just never covered this specific route.
    other = await client.get(f"/campuses/{uuid.uuid4()}", headers=target.headers)
    assert other.status_code == 403, other.text
    assert other.json()["detail"] == "account_suspended"


async def test_useroutput_schema_carries_the_suspended_timestamp(
    make_user: MakeUser,
) -> None:
    """The field mechanism itself, independent of any one route's enforcement
    choices: sets suspended_at directly and validates the ORM row straight
    through UserOut, the same from_attributes path every real response uses."""
    from app.db import get_session_factory
    from app.schemas.identity import UserOut

    target = await make_user("Directly Suspended")
    session_factory = get_session_factory()

    async with session_factory() as session:
        await session.execute(
            text("UPDATE users SET suspended_at = now() WHERE id = :id"),
            {"id": target.id},
        )
        await session.commit()

    from app import models

    async with session_factory() as session:
        row = await session.get(models.User, uuid.UUID(target.id))
        assert row is not None
        out = UserOut.model_validate(row)

    assert out.suspended_at is not None
