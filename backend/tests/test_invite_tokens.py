"""An invite code is a bounded credential, not a bearer token (board card c105).

What was true before: `chapter_invites` had a NULLABLE expires_at, no use count and
no revocation, and POST /chapters/join checked only existence and expiry. The default
way to mint a code produced one that worked forever, for anyone holding the string,
with no way to withdraw it short of an edit against the prod database.

c96 raised the stakes rather than creating them — redemption now writes
users.campus_id, and per c104 the only check on campus content is a literal
campus_id comparison, so the same forwarded string also mints campus identity.

These tests are written against the PROPERTIES, not the implementation: no code is
mintable without an expiry, a code stops working when its budget is spent, a revoked
code stops working immediately, and the budget cannot be oversubscribed by two
redemptions landing at once. The last one is the reason the router claims a seat with
a conditional UPDATE instead of counting one — the same read-check-then-write shape
that produced the dues double-charge.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from app.core.invites import (
    INVITE_DEFAULT_MAX_USES,
    INVITE_DEFAULT_TTL_DAYS,
    INVITE_MAX_TTL_DAYS,
    INVITE_MAX_USES_CAP,
)
from tests.conftest import MakeChapterWith, MakeUser


async def _mint(
    client: AsyncClient, setup, **body
) -> dict:
    """Mint an invite as the chapter president and return the response body."""
    payload = {"role": "member"}
    payload.update(body)
    response = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json=payload,
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _join(client: AsyncClient, code: str, user):
    return await client.post(
        "/chapters/join", json={"code": code}, headers=user.headers
    )


async def test_a_minted_code_always_expires(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The core c105 guarantee: there is no request that yields a forever-code.

    The old endpoint took expires_at as optional and stored it as given, so the
    documented default call produced NULL — a credential with no end.
    """
    invite = await _mint(client, await make_chapter_with("president"))

    assert invite["expires_at"] is not None
    expires = datetime.fromisoformat(invite["expires_at"])
    expected = datetime.now(timezone.utc) + timedelta(days=INVITE_DEFAULT_TTL_DAYS)
    assert abs((expires - expected).total_seconds()) < 300
    assert invite["max_uses"] == INVITE_DEFAULT_MAX_USES
    assert invite["uses"] == 0
    assert invite["revoked_at"] is None


async def test_an_over_long_expiry_is_clamped_not_honoured(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A caller asking for a year gets the policy ceiling, not a year.

    Clamped rather than rejected on purpose: the caller wanted a long-lived code and
    gets the longest one allowed, which fails toward the safe value instead of toward
    a 422 that invites someone to raise the cap.
    """
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    invite = await _mint(
        client,
        await make_chapter_with("president"),
        expires_at=far_future.isoformat(),
    )

    expires = datetime.fromisoformat(invite["expires_at"])
    ceiling = datetime.now(timezone.utc) + timedelta(days=INVITE_MAX_TTL_DAYS)
    assert expires <= ceiling + timedelta(minutes=5)
    assert expires < far_future


async def test_an_expiry_in_the_past_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Minting a dead-on-arrival code is safe but useless; say so instead of doing it."""
    setup = await make_chapter_with("president")
    response = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={
            "role": "member",
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat(),
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "invite_expiry_in_past"


async def test_max_uses_above_the_cap_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The cap is a bound, not a suggestion — schema-level, before it reaches the DB."""
    setup = await make_chapter_with("president")
    response = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": INVITE_MAX_USES_CAP + 1},
        headers=setup.president.headers,
    )
    assert response.status_code == 422, response.text


async def test_a_code_stops_working_once_its_budget_is_spent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The heart of the card: one redemption, not unlimited redemptions."""
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup, max_uses=1)

    first = await _join(client, invite["code"], await make_user("First Joiner"))
    assert first.status_code == 201, first.text

    second = await _join(client, invite["code"], await make_user("Second Joiner"))
    assert second.status_code == 403, second.text
    assert second.json()["detail"] == "invite_exhausted"


async def test_a_code_survives_up_to_its_budget(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The workflow this must NOT break: a president posts one code for a pledge class.

    Single-use would have secured the credential by making it useless for the way
    chapters actually onboard, so the budget has to be genuinely spendable.
    """
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup, max_uses=3)

    for index in range(3):
        joiner = await make_user(f"Pledge {index}")
        response = await _join(client, invite["code"], joiner)
        assert response.status_code == 201, response.text

    overflow = await _join(client, invite["code"], await make_user("One Too Many"))
    assert overflow.status_code == 403, overflow.text


async def test_a_rejoin_by_an_existing_member_does_not_burn_a_seat(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A double-tapped join button must not cost the chapter an invite seat.

    409 already_member is the response either way; the bug would be invisible from
    the outside and would quietly drain a pledge-class code.
    """
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup, max_uses=2)
    joiner = await make_user("Eager Joiner")

    assert (await _join(client, invite["code"], joiner)).status_code == 201
    again = await _join(client, invite["code"], joiner)
    assert again.status_code == 409, again.text

    # The second seat must still be there for someone who is not already a member.
    other = await _join(client, invite["code"], await make_user("Rightful Second"))
    assert other.status_code == 201, other.text


async def test_a_revoked_code_stops_working_immediately(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The answer to a code already loose in a group chat."""
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup, max_uses=10)

    revoked = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": invite["code"]},
        headers=setup.president.headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    blocked = await _join(client, invite["code"], await make_user("Holder Of Leak"))
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"] == "invite_revoked"


async def test_revoking_twice_is_idempotent(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The caller's intent is already satisfied; erroring would only confuse them."""
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup)
    url = f"/chapters/{setup.chapter_id}/invites/revoke"

    first = await client.post(
        url, json={"code": invite["code"]}, headers=setup.president.headers
    )
    second = await client.post(
        url, json={"code": invite["code"]}, headers=setup.president.headers
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


async def test_one_chapter_cannot_revoke_another_chapters_code(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """require_role proves you are e-board SOMEWHERE, which is not the same as here.

    Without the chapter_id in the WHERE clause, any president could kill any other
    chapter's invites by holding the string — a different flavour of the same
    bearer-token problem this card is about.
    """
    mine = await make_chapter_with("president")
    theirs = await make_chapter_with("president")
    victim = await _mint(client, theirs)

    response = await client.post(
        f"/chapters/{mine.chapter_id}/invites/revoke",
        json={"code": victim["code"]},
        headers=mine.president.headers,
    )
    assert response.status_code == 404, response.text

    # And the code still works, which is the part that actually matters.
    assert (
        await client.post(
            f"/chapters/{theirs.chapter_id}/invites/revoke",
            json={"code": victim["code"]},
            headers=theirs.president.headers,
        )
    ).status_code == 200


async def test_a_non_eboard_member_cannot_revoke(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Revocation is an e-board action; a plain member holding the code is not enough."""
    setup = await make_chapter_with("member")
    invite = await _mint(client, setup)

    response = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": invite["code"]},
        headers=setup.member.headers,
    )
    assert response.status_code == 403, response.text


async def test_revoking_an_unknown_code_is_a_404(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    response = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": f"nope-{uuid.uuid4().hex[:8]}"},
        headers=setup.president.headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "invite_not_found"


async def test_concurrent_redemptions_cannot_oversubscribe_the_budget(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The race the conditional UPDATE exists for.

    With a read-check-then-write on `uses`, four requests arriving together all read
    the same count, all decide there is room, and the last seat is sold twice. This
    test fires them concurrently against a budget of 2 and asserts exactly two get
    in. It is the invite-code shape of the dues double-charge, and it is worth an
    explicit test because the naive version passes every sequential test above.
    """
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup, max_uses=2)
    joiners = [await make_user(f"Racer {index}") for index in range(4)]

    responses = await asyncio.gather(
        *(_join(client, invite["code"], joiner) for joiner in joiners)
    )
    codes = sorted(response.status_code for response in responses)

    assert codes.count(201) == 2, [r.text for r in responses]
    assert codes.count(403) == 2, [r.text for r in responses]
