"""Capabilities come from the server, and the routers gate on the same sets (c80).

chapter/index.tsx used to hardcode roles: ["treasurer","president"] for its dues tile
while the invite card in the same file read the server's answer — one half of one
screen trusting the backend and the other half guessing. The guess was already wrong
(vice_president and historian are in EBOARD and got no tools at all) and nothing failed
when it drifted, because a client-side role list that disagrees with the server
produces either a 403 the user never sees or a tile that was never drawn.

The point of these tests is not that capabilities are returned. It is that the SAME
frozensets drive the route gate and the reported capability, so the two cannot drift.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.permissions import CAPABILITIES, DUES_ADMIN, MEMBERS_ADMIN, MINUTES_ADMIN
from tests.conftest import MakeChapterWith


async def _capabilities(client: AsyncClient, setup, user) -> list[str]:
    r = await client.get(f"/chapters/{setup.chapter_id}/role-meta", headers=user.headers)
    assert r.status_code == 200, r.text
    return r.json()["capabilities"]


async def test_president_holds_every_capability(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    assert sorted(await _capabilities(client, setup, setup.president)) == sorted(CAPABILITIES)


async def test_plain_member_holds_none(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    assert await _capabilities(client, setup, setup.member) == []


@pytest.mark.parametrize(
    "role,expected",
    [
        ("treasurer", {"dues_admin", "moderation", "lineage_admin"}),
        # polls_admin joined minutes_admin here in c162. Kept as a separate capability
        # rather than folded into minutes_admin: running a vote and writing the minutes
        # are different jobs that currently share an officer.
        ("secretary", {"minutes_admin", "polls_admin", "moderation", "lineage_admin"}),
        # The two roles the old hardcoded grid forgot entirely. They are e-board, so
        # they moderate — and they are NOT dues/minutes/members admins, which is the
        # half a "just show e-board everything" fix would have got wrong.
        # vice_president additionally holds deputy_overview (c163): read-only roster/
        # dues/invites data for the Deputy President dashboard, not a mutating
        # capability, and historian does not hold it.
        ("vice_president", {"moderation", "lineage_admin", "deputy_overview"}),
        # lineage_admin is EBOARD-wide (edit rights are "Historian/e-board", SPEC §7
        # m6), but the historian is the role the job is named for (c79).
        ("historian", {"moderation", "lineage_admin"}),
    ],
)
async def test_each_officer_gets_exactly_what_the_server_grants(
    client: AsyncClient, make_chapter_with: MakeChapterWith, role: str, expected: set[str]
) -> None:
    setup = await make_chapter_with(role)
    assert set(await _capabilities(client, setup, setup.member)) == expected


async def test_capability_sets_match_the_gates_they_are_named_for(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The anti-drift test, and the reason this file exists.

    A treasurer must both REPORT dues_admin and be ACCEPTED by a dues route; a
    secretary must be REFUSED by that same route. If someone edits DUES_ADMIN, both
    halves move together and this still passes. If someone reintroduces a literal
    role tuple at the gate, the two drift apart and this fails.
    """
    treasurer = await make_chapter_with("treasurer")
    secretary = await make_chapter_with("secretary")

    assert "dues_admin" in await _capabilities(client, treasurer, treasurer.member)
    assert "dues_admin" not in await _capabilities(client, secretary, secretary.member)

    allowed = await client.post(
        f"/chapters/{treasurer.chapter_id}/dues-cycles",
        json={"name": "Fall dues", "amount_cents": 5000, "due_date": "2026-10-01"},
        headers=treasurer.member.headers,
    )
    refused = await client.post(
        f"/chapters/{secretary.chapter_id}/dues-cycles",
        json={"name": "Fall dues", "amount_cents": 5000, "due_date": "2026-10-01"},
        headers=secretary.member.headers,
    )
    assert allowed.status_code == 201, allowed.text
    assert refused.status_code == 403, refused.text


def test_every_capability_name_maps_to_a_non_empty_role_set() -> None:
    """A capability nobody can hold would render a tile nobody ever sees, silently."""
    for name, roles in CAPABILITIES.items():
        assert roles, f"{name} maps to an empty role set"
    assert DUES_ADMIN and MINUTES_ADMIN and MEMBERS_ADMIN
