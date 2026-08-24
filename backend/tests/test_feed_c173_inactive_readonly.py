"""Inactive chapter members are READ-ONLY on the chapter-public tier (board c173,
ruled by Jose Aug 24 — reverses c102/#94 on this one point).

c102 (test_feed_actives_tier.py) widened the chapter-public ('org') READ gate to
admit inactive members and, on the theory that whoever can see a tier can also
act on it, let `_readable_post`'s 'org' branch accept active OR inactive
membership too — so an inactive member could like and comment on chapter-public
posts. Jose's ruling: inactive members are read-only, full stop. The READ gate
(list_posts / get_current_chapter_member) is untouched; only `_readable_post`'s
'org' branch tightens back to active-only, so like/comment/reaction endpoints
now refuse inactive members on the 'org' tier exactly as they already did on
'org_actives'.

Covers: an inactive member can still list the chapter feed and see the
chapter-public post, but gets 403 liking and 403 commenting on it; an active
member is unaffected on both; and org_actives tier visibility (a member never
sees it once inactive) is unchanged by this card.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def _create_post(
    client: AsyncClient, chapter_id: str, headers: dict[str, str], body: str, audience: str
) -> dict:
    response = await client.post(
        f"/chapters/{chapter_id}/posts", json={"body": body, "audience": audience}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _set_status(
    client: AsyncClient, chapter_id: str, president_headers: dict[str, str], user_id: str, status: str
) -> None:
    response = await client.patch(
        f"/chapters/{chapter_id}/members",
        json={"user_id": user_id, "status": status},
        headers=president_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status


async def test_inactive_member_can_read_but_not_react_to_chapter_public_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    public_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "chapter public", "org"
    )
    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )

    # Read gate (list_posts) is untouched by c173: still visible.
    listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert listing.status_code == 200, listing.text
    assert public_post["id"] in [p["id"] for p in listing.json()]

    # But _readable_post's 'org' branch now requires active membership: react is refused.
    like_denied = await client.put(
        f"/posts/{public_post['id']}/likes", headers=setup.member.headers
    )
    assert like_denied.status_code == 403, like_denied.text
    assert like_denied.json() == {"detail": "not_a_member"}

    comment_denied = await client.post(
        f"/posts/{public_post['id']}/comments",
        json={"body": "should fail"},
        headers=setup.member.headers,
    )
    assert comment_denied.status_code == 403, comment_denied.text
    assert comment_denied.json() == {"detail": "not_a_member"}


async def test_active_member_unaffected_liking_and_commenting_on_chapter_public_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Control: an active member's like/comment on the same 'org' post is untouched
    by c173 — this card only tightens the inactive case."""
    setup = await make_chapter_with("member")
    public_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "chapter public", "org"
    )

    liked = await client.put(f"/posts/{public_post['id']}/likes", headers=setup.member.headers)
    assert liked.status_code == 200, liked.text
    commented = await client.post(
        f"/posts/{public_post['id']}/comments",
        json={"body": "still fine"},
        headers=setup.member.headers,
    )
    assert commented.status_code == 201, commented.text


async def test_org_actives_tier_still_invisible_to_inactive_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c173 only touches the 'org' branch of `_readable_post` — org_actives read
    visibility (c102, list_posts' `visible_audiences`) is untouched: an inactive
    member still never sees an org_actives post in the chapter feed at all."""
    setup = await make_chapter_with("member")
    actives_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "actives only", "org_actives"
    )
    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )

    listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert listing.status_code == 200, listing.text
    assert actives_post["id"] not in [p["id"] for p in listing.json()]
