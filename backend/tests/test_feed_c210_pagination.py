"""GET /chapters/{chapter_id}/posts pagination (board card c210).

Same bug class as c127's fix to routers/chirps.py list_chirps: list_posts had no
limit at all, so a chapter with enough history returned its entire post table on
every load, each row also paying `_post_counts_select`'s three correlated
subqueries. Fixed with the same capped compound (created_at, id) cursor contract
as this file's sibling list_campus_feed (see tests/test_feed_audience.py's own
pagination section, which this file mirrors for the chapter-scoped route).

Covers: `limit` truncates the page and rejects out-of-range values with 422;
before/before_id pages through the whole set without overlap or loss; a tied
created_at at a page boundary is lossless; and the batched like/comment/
liked_by_me counts (c43) stay correct for a post on a paginated (non-first) page.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeChapterWith


async def _create_post(
    client: AsyncClient, chapter_id: str, headers: dict[str, str], body: str = "hello"
) -> dict:
    response = await client.post(
        f"/chapters/{chapter_id}/posts", json={"body": body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_posts_list_limit_is_capped_and_out_of_range_limit_is_422(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """`limit` truncates the page; values outside Query(ge=1, le=200) are 422."""
    setup = await make_chapter_with("member")
    for i in range(3):
        await _create_post(client, setup.chapter_id, setup.member.headers, f"post {i}")

    capped = await client.get(
        f"/chapters/{setup.chapter_id}/posts",
        params={"limit": 2},
        headers=setup.member.headers,
    )
    assert capped.status_code == 200, capped.text
    assert len(capped.json()) == 2

    too_big = await client.get(
        f"/chapters/{setup.chapter_id}/posts",
        params={"limit": 201},
        headers=setup.member.headers,
    )
    assert too_big.status_code == 422, too_big.text

    too_small = await client.get(
        f"/chapters/{setup.chapter_id}/posts",
        params={"limit": 0},
        headers=setup.member.headers,
    )
    assert too_small.status_code == 422, too_small.text


async def test_posts_list_pages_through_cursor_without_overlap_or_loss(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Paging with before/before_id across the whole set returns every post exactly
    once, in the same newest-first order as an unpaginated call."""
    setup = await make_chapter_with("member")
    created_ids: list[str] = []
    for i in range(5):
        post = await _create_post(client, setup.chapter_id, setup.member.headers, f"post {i}")
        created_ids.append(post["id"])

    full = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert full.status_code == 200, full.text
    expected_order = [p["id"] for p in full.json()]
    assert expected_order == list(reversed(created_ids))

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):  # generous cap so a regression can't spin the loop forever
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            f"/chapters/{setup.chapter_id}/posts",
            params=params,
            headers=setup.member.headers,
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(p["id"] for p in items)
        before = items[-1]["created_at"]
        before_id = items[-1]["id"]

    assert collected == expected_order


async def test_tied_created_at_at_page_boundary_is_lossless(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Several posts sharing one created_at, straddled across a page boundary: the
    compound (created_at, id) cursor must not drop any of them - same tie-break
    failure mode as messages.py's created_at cursor (SECURITY-REVIEW finding 10),
    already fixed on this file's list_campus_feed and mirrored here."""
    setup = await make_chapter_with("president")

    tied_at = datetime.now(timezone.utc)
    tied_ids: list[str] = []
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        for i in range(5):
            result = await session.execute(
                text(
                    "INSERT INTO posts"
                    " (chapter_id, campus_id, author_id, body, audience, created_at)"
                    " VALUES (:chapter_id, (SELECT campus_id FROM chapters WHERE"
                    " id = :chapter_id), :author_id, :body, 'org', :created_at)"
                    " RETURNING id"
                ),
                {
                    "chapter_id": uuid.UUID(setup.chapter_id),
                    "author_id": uuid.UUID(setup.president.id),
                    "body": f"tied {i}",
                    "created_at": tied_at,
                },
            )
            tied_ids.append(str(result.scalar_one()))
        await session.commit()

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            f"/chapters/{setup.chapter_id}/posts",
            params=params,
            headers=setup.president.headers,
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(p["id"] for p in items)
        before = items[-1]["created_at"]
        before_id = items[-1]["id"]

    assert len(collected) == len(set(collected)), "no duplicate rows across pages"
    assert set(collected) == set(tied_ids), (
        "compound (created_at, id) cursor must not drop or duplicate tied-timestamp rows"
    )


async def test_counts_still_correct_on_a_paginated_page(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """like_count/comment_count/liked_by_me (c43) stay correct for a post that only
    shows up on page two, not just for posts on the unpaginated first page."""
    setup = await make_chapter_with("member")

    # Older post first, so it lands on page two once the newer one pushes it off
    # page one at limit=1.
    older = await _create_post(client, setup.chapter_id, setup.member.headers, "older")
    liked = await client.put(f"/posts/{older['id']}/likes", headers=setup.member.headers)
    assert liked.status_code == 200, liked.text
    for i in range(2):
        commented = await client.post(
            f"/posts/{older['id']}/comments",
            json={"body": f"comment {i}"},
            headers=setup.president.headers,
        )
        assert commented.status_code == 201, commented.text

    newer = await _create_post(client, setup.chapter_id, setup.member.headers, "newer")

    first_page = await client.get(
        f"/chapters/{setup.chapter_id}/posts",
        params={"limit": 1},
        headers=setup.member.headers,
    )
    assert first_page.status_code == 200, first_page.text
    page_one = first_page.json()
    assert [p["id"] for p in page_one] == [newer["id"]]

    second_page = await client.get(
        f"/chapters/{setup.chapter_id}/posts",
        params={
            "limit": 1,
            "before": page_one[-1]["created_at"],
            "before_id": page_one[-1]["id"],
        },
        headers=setup.member.headers,
    )
    assert second_page.status_code == 200, second_page.text
    page_two = second_page.json()
    assert [p["id"] for p in page_two] == [older["id"]]
    row = page_two[0]
    assert row["like_count"] == 1
    assert row["comment_count"] == 2
    assert row["liked_by_me"] is True
