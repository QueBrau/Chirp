"""Actives-only chapter tier (board c102, ruled by Jose Aug 24).

'Active' means Membership.status == 'active' — the existing status flag, NOT a
new pledge/member model, NOT dues-paid standing. Chapter content splits into:

- 'org' (chapter-public): any non-removed member (active OR inactive) sees it.
- 'org_actives': only a viewer whose OWN membership.status == 'active' sees it.

Covers: active member sees both tiers; a non-active (status='inactive') member
sees only the public tier AND gets the X-Actives-Only-Hidden honest-signal header
— true only when actives-only content genuinely exists (never a static "yes"); a
non-member (no membership row) and a removed member both get 403; like/comment
gating on org_actives mirrors the read gate exactly; and create_post's existing
active-only entry gate is untouched by this feature (no accidental loosening of
who may WRITE at all).
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


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


async def test_active_member_sees_both_tiers(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    public_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "chapter public", "org"
    )
    actives_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "actives only", "org_actives"
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.president.headers
    )
    assert response.status_code == 200, response.text
    ids = [p["id"] for p in response.json()]
    assert public_post["id"] in ids
    assert actives_post["id"] in ids
    # Active viewer already sees everything — the honest signal must stay false.
    assert response.headers["x-actives-only-hidden"] == "false"


async def test_non_active_member_sees_only_public_tier_and_honest_signal(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    public_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "chapter public", "org"
    )
    actives_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "actives only", "org_actives"
    )
    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    ids = [p["id"] for p in response.json()]
    assert public_post["id"] in ids, "non-active member must still see the chapter-public tier"
    assert actives_post["id"] not in ids, "non-active member must never see org_actives content"
    # The honest signal (c102's named failure mode): a lesser-tier member must be
    # able to tell a fuller tier exists.
    assert response.headers["x-actives-only-hidden"] == "true"


async def test_honest_signal_is_false_when_no_actives_only_content_exists(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The signal is genuinely dynamic, not a hardcoded 'you're missing something':
    a chapter with zero org_actives posts must read as quiet, not as concealing."""
    setup = await make_chapter_with("member")
    await _create_post(client, setup.chapter_id, setup.president.headers, "public only", "org")
    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-actives-only-hidden"] == "false"


async def test_non_member_gets_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    outsider = await make_user("Outsider")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=outsider.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_a_member"}


async def test_removed_member_gets_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """'removed' is not a lesser tier — it is not a member at all."""
    setup = await make_chapter_with("member")
    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "removed"
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_a_member"}


async def test_like_and_comment_gating_matches_read_gating(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Write-adjacent (like/comment) gating on an org_actives post mirrors the read
    gate exactly: active can act on it, non-active cannot — but a non-active member
    can still like/comment on the chapter-public 'org' tier."""
    setup = await make_chapter_with("member")
    public_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "public", "org"
    )
    actives_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "actives", "org_actives"
    )

    # Still active: member can like/comment on both tiers.
    liked = await client.put(f"/posts/{actives_post['id']}/likes", headers=setup.member.headers)
    assert liked.status_code == 200, liked.text
    commented = await client.post(
        f"/posts/{actives_post['id']}/comments",
        json={"body": "nice"},
        headers=setup.member.headers,
    )
    assert commented.status_code == 201, commented.text

    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )

    # Now inactive: refused on the actives-only post...
    like_denied = await client.put(
        f"/posts/{actives_post['id']}/likes", headers=setup.member.headers
    )
    assert like_denied.status_code == 403, like_denied.text
    comment_denied = await client.post(
        f"/posts/{actives_post['id']}/comments",
        json={"body": "should fail"},
        headers=setup.member.headers,
    )
    assert comment_denied.status_code == 403, comment_denied.text

    # ...but still allowed on the chapter-public post, consistent with the read gate.
    like_allowed = await client.put(
        f"/posts/{public_post['id']}/likes", headers=setup.member.headers
    )
    assert like_allowed.status_code == 200, like_allowed.text
    comment_allowed = await client.post(
        f"/posts/{public_post['id']}/comments",
        json={"body": "still fine"},
        headers=setup.member.headers,
    )
    assert comment_allowed.status_code == 201, comment_allowed.text


async def test_create_post_still_requires_active_membership(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c102 only widens the READ gate (get_current_chapter_member). The WRITE gate
    (get_current_membership on create_post) is untouched — an inactive member still
    cannot author a post of ANY audience, org_actives included."""
    setup = await make_chapter_with("member")
    await _set_status(
        client, setup.chapter_id, setup.president.headers, setup.member.id, "inactive"
    )

    response = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "can I post?", "audience": "org"},
        headers=setup.member.headers,
    )
    assert response.status_code == 403, response.text
