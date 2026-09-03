"""c258 PR B: the ledger list is cursor-paginated, and it is safe to paginate.

SAFE BECAUSE NOTHING DERIVES A FIGURE FROM IT ANY MORE. PR A moved the treasurer's
balance, trend, category totals and dues meter to /ledger/summary, and this PR moved the
member's own "have I paid" to DuesCycleOut.viewer_paid. Before that, a page of this list
WAS the balance, and a payment row falling off a page made the app look like it had lost
someone's money.

The tie-break case is the reason for a COMPOUND cursor and is manufactured here rather
than hoped for: entries written in one burst share a created_at, and a timestamp-only
cursor drops or repeats them exactly at a page boundary.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import text as sa_text

from tests.conftest import MakeChapterWith

PAGE = 5


async def _seed_entries(setup, count: int, *, tied: bool = False) -> list[int]:
    from app.db import get_session_factory

    chapter_id = uuid.UUID(setup.chapter_id)
    author = uuid.UUID(setup.president.id)
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    amounts = [-(100 + i) for i in range(count)]
    ids = sorted(uuid.uuid4() for _ in range(count))
    async with get_session_factory()() as session:
        for i, (entry_id, amount) in enumerate(zip(ids, amounts)):
            await session.execute(
                sa_text(
                    "INSERT INTO ledger_entries (id, chapter_id, entry_type, amount_cents,"
                    " category, created_by, created_at)"
                    " VALUES (:id, :cid, 'expense', :amt, 'rush', :by, :at)"
                ),
                {
                    "id": entry_id, "cid": chapter_id, "amt": amount, "by": author,
                    # tied=True writes every row at ONE instant, which is what puts a
                    # page boundary inside a group sharing a timestamp.
                    "at": base if tied else base + timedelta(minutes=i),
                },
            )
        await session.commit()
    return amounts


async def _walk(client: AsyncClient, setup, page: int) -> list[int]:
    """Follow the cursor to exhaustion, newest-first, collecting amounts."""
    collected: list[int] = []
    cursor = ""
    for _ in range(20):  # bounded so a broken cursor cannot loop forever
        response = await client.get(
            f"/chapters/{setup.chapter_id}/ledger?limit={page}{cursor}",
            headers=setup.president.headers,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += [r["amount_cents"] for r in rows]
        cursor = f"&before={rows[-1]['created_at']}&before_id={rows[-1]['id']}"
        if len(rows) < page:
            break
    return collected


async def test_a_short_ledger_is_unchanged_by_pagination(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Every chapter today is far under one page; those must behave exactly as before."""
    setup = await make_chapter_with("member")
    amounts = await _seed_entries(setup, 4)

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    assert response.status_code == 200, response.text
    assert [r["amount_cents"] for r in response.json()] == list(reversed(amounts))


async def test_paging_walks_the_whole_ledger_without_gaps_or_repeats(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    amounts = await _seed_entries(setup, 13)

    collected = await _walk(client, setup, PAGE)
    assert collected == list(reversed(amounts)), "pages must reassemble the ledger exactly"


async def test_entries_sharing_a_timestamp_survive_a_page_boundary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The compound half. All 13 rows share ONE created_at, so a timestamp-only cursor
    either repeats or skips the entire tied run at the boundary."""
    setup = await make_chapter_with("member")
    amounts = await _seed_entries(setup, 13, tied=True)

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    stamps = {r["created_at"] for r in listed.json()}
    assert len(stamps) == 1, f"the ties must be real, got {stamps}"

    collected = await _walk(client, setup, PAGE)
    assert sorted(collected) == sorted(amounts), "no entry may be skipped or repeated"
    assert len(collected) == len(amounts)


async def test_the_summary_still_totals_the_WHOLE_ledger_while_the_list_pages(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The invariant that makes pagination safe, asserted rather than assumed: one page
    of the list is a fraction of the ledger, and the summary is still the whole thing."""
    setup = await make_chapter_with("member")
    amounts = await _seed_entries(setup, 13)

    page = await client.get(
        f"/chapters/{setup.chapter_id}/ledger?limit={PAGE}", headers=setup.president.headers
    )
    assert len(page.json()) == PAGE, "the page must genuinely be partial"

    summary = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
    )
    payload = summary.json()
    assert payload["entry_count"] == len(amounts)
    assert payload["balance_cents"] == sum(amounts), (
        "the balance must total the whole ledger, not the page"
    )


async def test_limit_is_bounded(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    for bad in (0, 5000):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/ledger?limit={bad}",
            headers=setup.president.headers,
        )
        assert response.status_code == 422, response.text
