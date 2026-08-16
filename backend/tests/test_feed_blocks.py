"""Blocked-author filtering on feed endpoints (board card c35): a Block button on
the feed is theater unless the server actually hides that author's posts. Before
this fix, routers/feed.py did not filter blocked authors at all (unlike
routers/yaks.py, tests/test_blocks.py) — a blocked person's posts kept appearing
on both GET /chapters/{chapter_id}/posts and GET /campuses/{campus_id}/feed.

Mirrors the anti-join proven for yaks: outerjoin user_blocks on
(blocked_id == author_id) AND (blocker_id == caller), then WHERE blocker_id IS
NULL. Two failure modes matter, per endpoint: a blocked author's posts vanish
for the blocker but stay visible to a bystander (the filter actually hides
something), and blocking author A never hides author B's posts (the anti-join
is scoped to the caller, not global — a missing blocker_id==caller condition
would hide EVERYONE's posts from EVERYONE, which no single-author test would
catch).
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import ApiUser, MakeChapterWith, MakeUser


async def _campus_id_of(client: AsyncClient, chapter_id: str, headers: dict[str, str]) -> str:
    response = await client.get(f"/chapters/{chapter_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["campus_id"]


async def _invite_and_join(
    client: AsyncClient,
    chapter_id: str,
    inviter_headers: dict[str, str],
    role: str,
    display_name: str,
    make_user: MakeUser,
) -> ApiUser:
    """Mint a fresh invite and redeem it as a new user — invite codes aren't
    single-use (no `used_at`/consumed marker on ChapterInvite), so a distinct
    invite per join isn't required, but minting one per call keeps each helper
    call self-contained."""
    invite = await client.post(
        f"/chapters/{chapter_id}/invites", json={"role": role}, headers=inviter_headers
    )
    assert invite.status_code == 201, invite.text
    member = await make_user(display_name)
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=member.headers
    )
    assert joined.status_code == 201, joined.text
    return member


async def _make_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Campus User"
) -> ApiUser:
    """Bootstrap a user pinned to `campus_id` (make_user doesn't expose campus_id).
    Matches the helper in test_feed_audience.py / test_blocks.py."""
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": email,
            "display_name": display_name,
            "account_type": "non_greek",
            "campus_id": campus_id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)


async def _block(client: AsyncClient, blocker_headers: dict[str, str], blocked_id: str) -> None:
    response = await client.post(
        "/moderation/blocks", json={"blocked_id": blocked_id}, headers=blocker_headers
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# GET /chapters/{chapter_id}/posts
# ---------------------------------------------------------------------------


async def test_blocked_authors_chapter_posts_hidden_from_blocker_visible_to_bystander(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    author = setup.member
    blocker = await _invite_and_join(
        client, setup.chapter_id, setup.president.headers, "member", "Blocker", make_user
    )
    bystander = await _invite_and_join(
        client, setup.chapter_id, setup.president.headers, "member", "Bystander", make_user
    )

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "hide me from blocker"},
        headers=author.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    await _block(client, blocker.headers, author.id)

    blocker_listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=blocker.headers
    )
    assert blocker_listing.status_code == 200, blocker_listing.text
    assert all(p["id"] != post_id for p in blocker_listing.json()), (
        "blocked author's post must be absent from the blocker's chapter feed"
    )

    bystander_listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=bystander.headers
    )
    assert bystander_listing.status_code == 200, bystander_listing.text
    assert any(p["id"] == post_id for p in bystander_listing.json()), (
        "a bystander who hasn't blocked anyone must still see the post"
    )


async def test_blocking_one_author_does_not_hide_another_authors_chapter_posts(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Proves the anti-join is scoped to the caller, not global: a missing
    blocker_id==caller condition would hide every author's posts from every
    caller, which the single-author test above would not catch."""
    setup = await make_chapter_with("member")
    author_a = setup.member
    author_b = await _invite_and_join(
        client, setup.chapter_id, setup.president.headers, "member", "Author B", make_user
    )
    blocker = await _invite_and_join(
        client, setup.chapter_id, setup.president.headers, "member", "Blocker", make_user
    )

    post_a = await client.post(
        f"/chapters/{setup.chapter_id}/posts", json={"body": "from a"}, headers=author_a.headers
    )
    assert post_a.status_code == 201, post_a.text
    post_b = await client.post(
        f"/chapters/{setup.chapter_id}/posts", json={"body": "from b"}, headers=author_b.headers
    )
    assert post_b.status_code == 201, post_b.text

    await _block(client, blocker.headers, author_a.id)

    listing = await client.get(f"/chapters/{setup.chapter_id}/posts", headers=blocker.headers)
    assert listing.status_code == 200, listing.text
    ids = {p["id"] for p in listing.json()}
    assert post_a.json()["id"] not in ids, "blocked author A's post must be hidden"
    assert post_b.json()["id"] in ids, "author B's post must NOT be hidden by A's block"


# ---------------------------------------------------------------------------
# GET /campuses/{campus_id}/feed
# ---------------------------------------------------------------------------


async def test_blocked_authors_campus_posts_hidden_from_blocker_visible_to_bystander(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "campus post to hide", "audience": "campus"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    blocker = await _make_campus_user(client, campus_id, "Blocker")
    bystander = await _make_campus_user(client, campus_id, "Bystander")

    await _block(client, blocker.headers, setup.president.id)

    blocker_feed = await client.get(f"/campuses/{campus_id}/feed", headers=blocker.headers)
    assert blocker_feed.status_code == 200, blocker_feed.text
    assert all(p["id"] != post_id for p in blocker_feed.json()), (
        "blocked author's post must be absent from the blocker's campus feed"
    )

    bystander_feed = await client.get(f"/campuses/{campus_id}/feed", headers=bystander.headers)
    assert bystander_feed.status_code == 200, bystander_feed.text
    assert any(p["id"] == post_id for p in bystander_feed.json()), (
        "a bystander who hasn't blocked anyone must still see the post"
    )


async def test_blocking_one_author_does_not_hide_another_authors_campus_posts(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Same anti-join scoping proof as the chapter-feed test above, applied to the
    campus feed's separate query."""
    setup = await make_chapter_with("member")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    author_a = setup.president
    author_b = setup.member

    post_a = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "campus from a", "audience": "campus"},
        headers=author_a.headers,
    )
    assert post_a.status_code == 201, post_a.text
    post_b = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "campus from b", "audience": "campus"},
        headers=author_b.headers,
    )
    assert post_b.status_code == 201, post_b.text

    blocker = await _make_campus_user(client, campus_id, "Blocker")
    await _block(client, blocker.headers, author_a.id)

    feed = await client.get(f"/campuses/{campus_id}/feed", headers=blocker.headers)
    assert feed.status_code == 200, feed.text
    ids = {p["id"] for p in feed.json()}
    assert post_a.json()["id"] not in ids, "blocked author A's campus post must be hidden"
    assert post_b.json()["id"] in ids, "author B's campus post must NOT be hidden by A's block"
