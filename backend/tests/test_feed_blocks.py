"""Blocked-author filtering on feed endpoints (board card c35): a Block button on
the feed is theater unless the server actually hides that author's posts. Before
this fix, routers/feed.py did not filter blocked authors at all (unlike
routers/chirps.py, tests/test_blocks.py) — a blocked person's posts kept appearing
on both GET /chapters/{chapter_id}/posts and GET /campuses/{campus_id}/feed.

Mirrors the anti-join proven for chirps: outerjoin user_blocks on
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

from tests.conftest import ApiUser, MakeChapterWith, MakeUser, set_campus, verify_campus


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
    """Bootstrap a user, then pin them to `campus_id` directly.

    c85 made campus SERVER-OWNED: POST /auth/bootstrap no longer accepts campus_id
    in the body, so passing it there is silently ignored and the user ends up with
    no campus at all — which the campus-feed guard then correctly 403s. Matches the
    helper in test_feed_audience.py; set_campus writes the column the way tests
    already set is_platform_admin, until the .edu redemption (c86) is the real writer.
    """
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": email,
            "display_name": display_name,
            "account_type": "non_greek",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    await set_campus(user.id, campus_id)
    return user


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


# ---------------------------------------------------------------------------
# Self-block (board card c237)
# ---------------------------------------------------------------------------


async def test_self_block_is_refused_with_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """POST /moderation/blocks must refuse the caller's own id.

    It used to accept it, while POST /moderation/blocks/by-chirp has always refused
    (403 cannot_block_self, tests/test_blocks.py) - the same act was legal through one
    endpoint and forbidden through the other. Asserted as the same status AND the same
    detail string, since a second spelling of this refusal is the thing that lets the
    two drift apart again.
    """
    setup = await make_chapter_with("member")

    response = await client.post(
        "/moderation/blocks", json={"blocked_id": setup.member.id}, headers=setup.member.headers
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "cannot_block_self"}


async def test_a_refused_self_block_leaves_your_own_posts_on_your_own_feed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The symptom c237 is actually about, asserted end to end rather than inferred.

    The anti-join above hides posts whose author the caller has blocked, and it does
    not exempt the caller - so a stored self-block takes the user's OWN posts off
    their OWN feed, which reads as data loss rather than as a moderation setting.
    This drives the whole path: post, attempt the self-block, and confirm the post is
    still there. It fails if the guard is removed, because the block would then be
    stored and the anti-join would hide the post.
    """
    setup = await make_chapter_with("member")
    author = setup.member

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "still mine"},
        headers=author.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    refused = await client.post(
        "/moderation/blocks", json={"blocked_id": author.id}, headers=author.headers
    )
    assert refused.status_code == 403, refused.text

    listing = await client.get(f"/chapters/{setup.chapter_id}/posts", headers=author.headers)
    assert listing.status_code == 200, listing.text
    assert any(p["id"] == post_id for p in listing.json()), (
        "a user's own post must stay on their own feed after a refused self-block"
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
    # c88: a campus-audience post needs a proved .edu, not just a chapter campus.
    await verify_campus(setup.president.id)

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
    await verify_campus(author_a.id)
    await verify_campus(author_b.id)

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
