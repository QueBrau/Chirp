"""c258 Bucket A: GET /posts/{post_id}/comments is cursor-paginated.

Comments grow with TIME, not with a roster, so a cap alone would just move the
truncation later - this is the cursor case, unlike PR 1's roster-bounded lists.

THE DESIGN, and these tests are what pin it:
  - the DEFAULT page is the NEWEST comments, because a thread continues at the
    bottom and that is where CommentsSheet puts its composer;
  - the RESPONSE stays oldest-first, so the existing render path is untouched and
    a thread shorter than `limit` returns exactly what it always did;
  - `before`/`before_id` take the OLDEST comment you hold and return the ones
    before it, which the client prepends.

The tie-break case is the one that matters most and is the reason for a COMPOUND
cursor: comments posted inside the same clock tick share a created_at, and a
timestamp-only cursor either skips them or repeats them at the page boundary.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def _post_with_comments(
    client: AsyncClient, make_chapter_with: MakeChapterWith, count: int
) -> tuple[str, dict[str, str], list[str]]:
    setup = await make_chapter_with("member")
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "thread"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    bodies = [f"comment-{i:03d}" for i in range(count)]
    for body in bodies:
        created = await client.post(
            f"/posts/{post_id}/comments", json={"body": body}, headers=setup.president.headers
        )
        assert created.status_code == 201, created.text
    return post_id, setup.president.headers, bodies


async def test_a_short_thread_is_unchanged_by_pagination(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Every thread today is shorter than the default page. Those must be byte-identical
    to the pre-cursor behaviour: all comments, oldest first, one request."""
    post_id, headers, bodies = await _post_with_comments(client, make_chapter_with, 5)

    response = await client.get(f"/posts/{post_id}/comments", headers=headers)
    assert response.status_code == 200, response.text
    assert [c["body"] for c in response.json()] == bodies


async def test_the_default_page_is_the_newest_comments_in_reading_order(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The whole point of the design: on a long thread the default page is the RECENT
    end, not the oldest, but still ascending so it renders as a thread."""
    post_id, headers, bodies = await _post_with_comments(client, make_chapter_with, 12)

    response = await client.get(f"/posts/{post_id}/comments?limit=5", headers=headers)
    assert response.status_code == 200, response.text
    got = [c["body"] for c in response.json()]
    assert got == bodies[-5:], "expected the last five, oldest-first within the page"


async def test_paging_backwards_walks_the_whole_thread_without_gaps_or_repeats(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Follow the cursor to exhaustion and reassemble - the union must be the thread
    exactly once, in order. A gap or a duplicate here is the bug the compound cursor
    exists to prevent."""
    post_id, headers, bodies = await _post_with_comments(client, make_chapter_with, 12)

    collected: list[str] = []
    cursor = ""
    for _ in range(10):  # bounded so a broken cursor cannot loop forever
        response = await client.get(
            f"/posts/{post_id}/comments?limit=5{cursor}", headers=headers
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        collected = [c["body"] for c in page] + collected
        cursor = f"&before={page[0]['created_at']}&before_id={page[0]['id']}"
        if len(page) < 5:
            break

    assert collected == bodies, "the pages must reassemble into the thread exactly"


async def test_comments_sharing_a_timestamp_are_not_skipped_at_a_page_boundary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The compound half of the cursor, with REAL ties.

    THIS TEST WAS VACUOUS ON ITS FIRST WRITING and the falsification pass is what
    caught it: comments created over HTTP get distinct microsecond timestamps, so
    removing the (created_at, id) tie-break left every test green. Ties have to be
    MANUFACTURED to prove the compound cursor does anything, so these six rows are
    written straight to the database sharing one created_at, three per page, which
    puts a boundary in the middle of a tied group. With a timestamp-only cursor the
    second page re-reads or skips the whole tied run; only the id tie-break walks it
    exactly once.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    from sqlalchemy import text as sa_text

    setup = await make_chapter_with("member")
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "tied thread"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    from app.db import get_session_factory

    tied_at = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids = sorted(_uuid.uuid4() for _ in range(6))
    bodies = [f"tied-{i}" for i in range(6)]
    async with get_session_factory()() as session:
        for comment_id, body in zip(ids, bodies):
            await session.execute(
                sa_text(
                    "INSERT INTO post_comments (id, post_id, author_id, body, created_at)"
                    " VALUES (:id, :post_id, :author_id, :body, :created_at)"
                ),
                {
                    "id": comment_id,
                    "post_id": _uuid.UUID(post_id),
                    "author_id": _uuid.UUID(setup.president.id),
                    "body": body,
                    "created_at": tied_at,
                },
            )
        await session.commit()

    # Sanity: the ties are real, otherwise this test proves nothing again.
    first = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    assert first.status_code == 200, first.text
    stamps = {c["created_at"] for c in first.json()}
    assert len(stamps) == 1, f"expected one shared timestamp, got {stamps}"

    collected: list[str] = []
    cursor = ""
    for _ in range(10):
        response = await client.get(
            f"/posts/{post_id}/comments?limit=3{cursor}", headers=setup.president.headers
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        collected = [c["body"] for c in page] + collected
        cursor = f"&before={page[0]['created_at']}&before_id={page[0]['id']}"
        if len(page) < 3:
            break

    assert collected == bodies, (
        "every tied comment must appear exactly once, in id order, across the boundary"
    )


async def test_limit_is_bounded(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The cap still exists behind the cursor - a caller cannot ask for everything."""
    post_id, headers, _ = await _post_with_comments(client, make_chapter_with, 2)

    too_big = await client.get(f"/posts/{post_id}/comments?limit=5000", headers=headers)
    assert too_big.status_code == 422, too_big.text

    zero = await client.get(f"/posts/{post_id}/comments?limit=0", headers=headers)
    assert zero.status_code == 422, zero.text
