"""GET /chapters/{chapter_id}/posts/count — the caller's own post count (board c217).

The profile screen's "posts by me" stat used to be a client-side filter over the
whole of GET /chapters/{id}/posts. c210 capped that route at 50 with a cursor, so
past 50 posts the screen only ever saw page one and the stat silently undercounted.
The fix is a count endpoint, not an un-capped list (see the route's docstring).

Covers: the count agrees with a member's posts gathered across MORE than one page
(the file seeds past the 50-item cap, so the old client-side filter would visibly
have been wrong); soft-deleted posts are excluded; the c102 audience tiers are
honored, so an inactive member's own org_actives posts stop counting exactly when
they stop being listed; the count is self-scoped, never picking up another
member's posts; and the entry gate is list_posts' own (member-only, active or
inactive).
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser

# One past c210's default page size, so page one alone is provably not the answer.
PAST_ONE_PAGE = 55


async def _create_post(
    client: AsyncClient,
    chapter_id: str,
    headers: dict[str, str],
    body: str = "hello",
    audience: str = "org",
) -> dict:
    response = await client.post(
        f"/chapters/{chapter_id}/posts",
        json={"body": body, "audience": audience},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _set_status(
    client: AsyncClient,
    chapter_id: str,
    president_headers: dict[str, str],
    user_id: str,
    status: str,
) -> None:
    response = await client.patch(
        f"/chapters/{chapter_id}/members",
        json={"user_id": user_id, "status": status},
        headers=president_headers,
    )
    assert response.status_code == 200, response.text


async def _my_count(client: AsyncClient, chapter_id: str, headers: dict[str, str]) -> int:
    response = await client.get(f"/chapters/{chapter_id}/posts/count", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["chapter_id"] == chapter_id
    return payload["count"]


async def _list_all_by_author(
    client: AsyncClient, chapter_id: str, headers: dict[str, str], author_id: str
) -> list[str]:
    """Every post by `author_id` the caller can see, paged through with c210's cursor.

    This is what the profile screen would have to do to get an honest number out of
    the list route, and it is precisely what the count endpoint exists to avoid.
    """
    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(20):  # generous cap so a regression cannot spin forever
        params: dict[str, str | int] = {"limit": 50}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            f"/chapters/{chapter_id}/posts", params=params, headers=headers
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(p["id"] for p in items if p["author_id"] == author_id)
        before = items[-1]["created_at"]
        before_id = items[-1]["id"]
    return collected


async def test_count_matches_a_listing_across_more_than_one_page(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The bug in one assertion: past c210's 50-item cap, page one alone undercounts
    and the count endpoint does not."""
    setup = await make_chapter_with("member")
    for i in range(PAST_ONE_PAGE):
        await _create_post(client, setup.chapter_id, setup.member.headers, f"mine {i}")

    paged = await _list_all_by_author(
        client, setup.chapter_id, setup.member.headers, setup.member.id
    )
    assert len(paged) == PAST_ONE_PAGE

    # The old client-side filter, verbatim: one uncursored call, filtered in memory.
    first_page = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert first_page.status_code == 200, first_page.text
    old_way = [p for p in first_page.json() if p["author_id"] == setup.member.id]
    assert len(old_way) == 50, "c210 caps the first page — this is the undercount"

    assert await _my_count(client, setup.chapter_id, setup.member.headers) == PAST_ONE_PAGE


async def test_count_excludes_soft_deleted_posts(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Deleting a post must decrement the stat on the same load it leaves the feed."""
    setup = await make_chapter_with("member")
    kept = await _create_post(client, setup.chapter_id, setup.member.headers, "kept")
    doomed = await _create_post(client, setup.chapter_id, setup.member.headers, "doomed")
    assert await _my_count(client, setup.chapter_id, setup.member.headers) == 2

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/posts/{doomed['id']}", headers=setup.member.headers
    )
    assert deleted.status_code == 204, deleted.text

    assert await _my_count(client, setup.chapter_id, setup.member.headers) == 1
    assert await _list_all_by_author(
        client, setup.chapter_id, setup.member.headers, setup.member.id
    ) == [kept["id"]]


async def test_count_honors_the_audience_tier_of_the_caller(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c102: an inactive member's own org_actives posts stop counting exactly when
    they stop being listed. A stat that counts rows the member cannot see is the
    honest-signal failure in miniature — and a campus post is in neither number."""
    setup = await make_chapter_with("member")
    await _create_post(client, setup.chapter_id, setup.member.headers, "public", "org")
    await _create_post(
        client, setup.chapter_id, setup.member.headers, "actives", "org_actives"
    )
    assert await _my_count(client, setup.chapter_id, setup.member.headers) == 2

    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )
    assert await _my_count(client, setup.chapter_id, setup.member.headers) == 1
    assert (
        len(
            await _list_all_by_author(
                client, setup.chapter_id, setup.member.headers, setup.member.id
            )
        )
        == 1
    ), "the count and the listing must agree about the tier, not just individually"


async def test_count_is_self_scoped_and_ignores_other_members_posts(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The author filter comes from the auth dependency: two members in one chapter
    get two different numbers out of the same URL, and neither sees the other's."""
    setup = await make_chapter_with("member")
    for i in range(3):
        await _create_post(client, setup.chapter_id, setup.member.headers, f"mine {i}")
    for i in range(7):
        await _create_post(client, setup.chapter_id, setup.president.headers, f"theirs {i}")

    assert await _my_count(client, setup.chapter_id, setup.member.headers) == 3
    assert await _my_count(client, setup.chapter_id, setup.president.headers) == 7


async def test_count_entry_gate_matches_list_posts(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Non-members are refused (403), and an INACTIVE member is still let in — the
    same deliberately-looser gate list_posts uses, not the active-only one."""
    setup = await make_chapter_with("member")
    await _create_post(client, setup.chapter_id, setup.member.headers, "public", "org")

    outsider = await make_user("Some Other Student")
    refused = await client.get(
        f"/chapters/{setup.chapter_id}/posts/count", headers=outsider.headers
    )
    assert refused.status_code == 403, refused.text

    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )
    assert await _my_count(client, setup.chapter_id, setup.member.headers) == 1
