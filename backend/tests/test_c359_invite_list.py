"""GET /chapters/{chapter_id}/invites gains a cursor past the 200-row cap (c359).

Before this change the route was a flat .limit(MAX_HISTORY_PAGE)=200 with only a log
warning past that - a chapter with more than 200 live/dead codes had codes that were
neither visible nor revocable at all, since minting is the only place a code is ever
returned (c111) and this list is the only other way to reach one.

Invites are bulk-inserted directly rather than minted through POST .../invites: the
real route is rate-limited to INVITE_MINT_LIMIT=30/hour (c259), which 200+ real mints
in one test would hit long before proving anything about the READ side this card
changes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import insert

from app import models
from app.db import get_session_factory
from tests.conftest import MakeChapterWith

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


async def _seed_invites(chapter_id: str, created_by: str, expires_ats: list[datetime]) -> list[str]:
    """Bulk-insert one ChapterInvite per given expires_at; returns the minted codes."""
    codes = [f"c359-{uuid.uuid4().hex}" for _ in expires_ats]
    async with get_session_factory()() as session:
        await session.execute(insert(models.ChapterInvite), [
            {
                "id": uuid.uuid4(),
                "chapter_id": uuid.UUID(chapter_id),
                "code": code,
                "role": "member",
                "expires_at": expires_at,
                "max_uses": 25,
                "uses": 0,
                "created_by": uuid.UUID(created_by),
            }
            for code, expires_at in zip(codes, expires_ats)
        ])
        await session.commit()
    return codes


async def _walk_invites(client: AsyncClient, chapter_id: str, headers: dict, page: int) -> list[dict]:
    collected: list[dict] = []
    cursor = ""
    for _ in range(30):  # generous cap so a regression can't spin the loop forever
        response = await client.get(
            f"/chapters/{chapter_id}/invites?limit={page}{cursor}", headers=headers,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += rows
        cursor = f"&before={rows[-1]['expires_at']}&before_id={rows[-1]['id']}"
        if len(rows) < page:
            break
    return collected


async def test_more_than_200_codes_stay_discoverable_and_revocable(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    expires_ats = [BASE + timedelta(minutes=i) for i in range(205)]
    minted = await _seed_invites(setup.chapter_id, setup.president.id, expires_ats)

    collected = await _walk_invites(client, setup.chapter_id, setup.president.headers, 50)

    assert len(collected) == 205, "every minted code must be reachable, not just the first 200"
    assert {row["code"] for row in collected} == set(minted), (
        "the union of all pages must equal every minted code - proving the 201st "
        "through 205th codes (unreachable before the cursor) are now reachable"
    )

    # A code from the LAST page (furthest from the cap, would never have been seen
    # under the old flat-200 route) must still be revocable.
    target_code = collected[-1]["code"]
    revoked = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": target_code},
        headers=setup.president.headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None


async def test_tied_expires_at_page_boundary_is_lossless(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """expires_at alone cannot disambiguate a tied group; the id tie-break must."""
    setup = await make_chapter_with("president")
    tied_at = BASE + timedelta(days=1)
    minted = await _seed_invites(setup.chapter_id, setup.president.id, [tied_at] * 9)

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers,
    )
    assert len({row["expires_at"] for row in listed.json()}) == 1, "the ties must be real"

    collected = await _walk_invites(client, setup.chapter_id, setup.president.headers, 3)

    codes = [row["code"] for row in collected]
    assert sorted(codes) == sorted(minted), "no tied code may be skipped or repeated"
    assert len(codes) == len(minted)
