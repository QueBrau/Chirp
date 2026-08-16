"""Campus feed (board card c34): audience privacy, batched counts, pagination.

Covers: 'org' posts never leak onto the campus feed (the endpoint's core privacy
guarantee), campus scoping (403 for non-members, isolation across campuses),
batched like/comment counts via scalar subqueries (the cartesian-join regression
a join-and-COUNT(DISTINCT) would produce), liked_by_me per caller, the compound
(created_at, id) pagination cursor, the 'org' default when audience is omitted,
and soft-delete exclusion for both posts and comments.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeChapterWith, MakeUser, set_campus, verify_campus


async def _make_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Campus User"
) -> ApiUser:
    """Bootstrap a user pinned to `campus_id` (make_user doesn't expose campus_id).

    Matches the helper in test_yaks.py / test_moderation_scope.py.
    """
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": email,
            "display_name": display_name,
            "account_type": "greek",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    # c85: campus is server-owned, so it is set directly rather than claimed in the
    # bootstrap body. Same pattern as _grant_platform_admin — no API grants it until
    # the .edu redemption in c86 exists.
    await set_campus(user.id, campus_id)
    return user


async def _campus_id_of(client: AsyncClient, chapter_id: str, headers: dict[str, str]) -> str:
    response = await client.get(f"/chapters/{chapter_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["campus_id"]


async def _create_post(
    client: AsyncClient,
    chapter_id: str,
    headers: dict[str, str],
    body: str = "hello",
    audience: str | None = "org",
) -> dict:
    payload: dict = {"body": body}
    if audience is not None:
        payload["audience"] = audience
    response = await client.post(
        f"/chapters/{chapter_id}/posts", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Core privacy guarantee
# ---------------------------------------------------------------------------


async def test_org_post_never_appears_in_campus_feed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A chapter's 'org' post never surfaces on GET /campuses/{id}/feed; the
    chapter's 'campus' post does. This is the endpoint's core privacy guarantee.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    viewer = await _make_campus_user(client, campus_id, "Feed Viewer")

    org_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "chapter-only", audience="org"
    )
    # c88: posting campus-wide requires a proved .edu, not merely a chapter campus.
    await verify_campus(setup.president.id)
    campus_post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "campus-wide", audience="campus"
    )

    response = await client.get(f"/campuses/{campus_id}/feed", headers=viewer.headers)
    assert response.status_code == 200, response.text
    ids = [p["id"] for p in response.json()]

    assert campus_post["id"] in ids, "campus-audience post must appear in the campus feed"
    assert org_post["id"] not in ids, "org-audience post must NEVER appear in the campus feed"


async def test_non_member_of_campus_gets_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A caller whose users.campus_id doesn't match the path campus gets 403."""
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    outsider = await make_user("No Campus")  # campus_id is NULL for a plain make_user

    response = await client.get(f"/campuses/{campus_id}/feed", headers=outsider.headers)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_your_campus"}


async def test_cross_campus_isolation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Campus A's feed never contains campus B's posts, even though both are
    'campus'-audience."""
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("president")
    campus_a = await _campus_id_of(client, chapter_a.chapter_id, chapter_a.president.headers)
    campus_b = await _campus_id_of(client, chapter_b.chapter_id, chapter_b.president.headers)
    assert campus_a != campus_b, "make_chapter_with should mint a fresh campus each call"

    await verify_campus(chapter_a.president.id)
    await verify_campus(chapter_b.president.id)
    post_a = await _create_post(
        client, chapter_a.chapter_id, chapter_a.president.headers, "campus A post", "campus"
    )
    post_b = await _create_post(
        client, chapter_b.chapter_id, chapter_b.president.headers, "campus B post", "campus"
    )

    viewer_a = await _make_campus_user(client, campus_a, "Viewer A")
    feed_a = await client.get(f"/campuses/{campus_a}/feed", headers=viewer_a.headers)
    assert feed_a.status_code == 200, feed_a.text
    ids_a = [p["id"] for p in feed_a.json()]
    assert post_a["id"] in ids_a
    assert post_b["id"] not in ids_a, "campus A feed must never contain campus B's posts"


# ---------------------------------------------------------------------------
# Batched counts (c43) — the cartesian-join regression
# ---------------------------------------------------------------------------


async def test_counts_correct_with_multiple_likes_and_comments(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A post with 3 likes AND 4 comments reports like_count=3, comment_count=4 —
    NOT 12. A join-and-COUNT(DISTINCT) over both one-to-many tables would fan out
    to 3*4=12 rows before counting and silently corrupt this.
    """
    setup = await make_chapter_with("member")
    post = await _create_post(client, setup.chapter_id, setup.member.headers, "count me")
    post_id = post["id"]

    likers = [setup.president, setup.member]  # 2 distinct likers so far
    third_liker_invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert third_liker_invite.status_code == 201, third_liker_invite.text
    third_liker = await make_user("Third Liker")
    joined = await client.post(
        "/chapters/join",
        json={"code": third_liker_invite.json()["code"]},
        headers=third_liker.headers,
    )
    assert joined.status_code == 201, joined.text
    likers.append(third_liker)
    assert len(likers) == 3

    for liker in likers:
        liked = await client.put(f"/posts/{post_id}/likes", headers=liker.headers)
        assert liked.status_code == 200, liked.text

    for i in range(4):
        commented = await client.post(
            f"/posts/{post_id}/comments",
            json={"body": f"comment {i}"},
            headers=setup.member.headers,
        )
        assert commented.status_code == 201, commented.text

    listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert listing.status_code == 200, listing.text
    row = next(p for p in listing.json() if p["id"] == post_id)
    assert row["like_count"] == 3, f"expected 3 likes, got {row['like_count']}"
    assert row["comment_count"] == 4, f"expected 4 comments, got {row['comment_count']}"


async def test_counts_present_on_campus_feed_too(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The campus feed carries the same batched-count shape as the chapter listing."""
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    await verify_campus(setup.president.id)
    post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "campus counted", "campus"
    )
    liked = await client.put(f"/posts/{post['id']}/likes", headers=setup.president.headers)
    assert liked.status_code == 200, liked.text

    viewer = await _make_campus_user(client, campus_id, "Counter Viewer")
    response = await client.get(f"/campuses/{campus_id}/feed", headers=viewer.headers)
    assert response.status_code == 200, response.text
    row = next(p for p in response.json() if p["id"] == post["id"])
    assert row["like_count"] == 1
    assert row["comment_count"] == 0
    assert row["display_name"] == "Chapter President"
    # The president liked it, but the CALLER here is a different viewer who did not.
    assert row["liked_by_me"] is False


async def test_liked_by_me_is_per_caller(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """liked_by_me reflects the CALLER's own like, not global like state."""
    setup = await make_chapter_with("member")
    post = await _create_post(client, setup.chapter_id, setup.member.headers, "like me")
    liked = await client.put(
        f"/posts/{post['id']}/likes", headers=setup.president.headers
    )
    assert liked.status_code == 200, liked.text

    as_liker = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.president.headers
    )
    as_other = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    liker_row = next(p for p in as_liker.json() if p["id"] == post["id"])
    other_row = next(p for p in as_other.json() if p["id"] == post["id"])
    assert liker_row["liked_by_me"] is True
    assert other_row["liked_by_me"] is False


# ---------------------------------------------------------------------------
# Pagination: compound (created_at, id) cursor
# ---------------------------------------------------------------------------


async def test_campus_feed_pagination_lossless_across_tied_timestamp(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """5 same-timestamp campus posts: paging with before+before_id returns all 5,
    no dupes/gaps. Mirrors tests/test_pagination.py's message cursor test exactly.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    viewer = await _make_campus_user(client, campus_id, "Pager Viewer")

    tied_at = datetime.now(timezone.utc)
    post_ids: list[str] = []
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        for i in range(5):
            result = await session.execute(
                text(
                    "INSERT INTO posts (chapter_id, author_id, body, audience, created_at)"
                    " VALUES (:chapter_id, :author_id, :body, 'campus', :created_at)"
                    " RETURNING id"
                ),
                {
                    "chapter_id": uuid.UUID(setup.chapter_id),
                    "author_id": uuid.UUID(setup.president.id),
                    "body": f"tied {i}",
                    "created_at": tied_at,
                },
            )
            post_ids.append(str(result.scalar_one()))
        await session.commit()

    seen: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):  # generous cap so a regression can't spin the loop forever
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        response = await client.get(
            f"/campuses/{campus_id}/feed", params=params, headers=viewer.headers
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        seen.extend(p["id"] for p in page)
        before = page[-1]["created_at"]
        before_id = page[-1]["id"]

    assert sorted(seen) == sorted(post_ids), (
        "compound (created_at, id) cursor must not drop or duplicate tied-timestamp rows"
    )


# ---------------------------------------------------------------------------
# Safe default + soft-delete exclusion
# ---------------------------------------------------------------------------


async def test_audience_defaults_to_org_when_omitted(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A client that omits `audience` gets a private 'org' post — never campus-wide."""
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)

    post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "no audience field", audience=None
    )
    assert post["audience"] == "org"

    viewer = await _make_campus_user(client, campus_id, "Default Viewer")
    feed = await client.get(f"/campuses/{campus_id}/feed", headers=viewer.headers)
    assert feed.status_code == 200, feed.text
    assert all(p["id"] != post["id"] for p in feed.json()), (
        "a post created without an audience field must never appear on the campus feed"
    )


async def test_soft_deleted_post_excluded_from_both_feeds(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    await verify_campus(setup.president.id)
    post = await _create_post(
        client, setup.chapter_id, setup.president.headers, "will be deleted", "campus"
    )

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/posts/{post['id']}", headers=setup.president.headers
    )
    assert deleted.status_code == 204, deleted.text

    chapter_listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.president.headers
    )
    assert all(p["id"] != post["id"] for p in chapter_listing.json())

    viewer = await _make_campus_user(client, campus_id, "Delete Viewer")
    campus_feed = await client.get(f"/campuses/{campus_id}/feed", headers=viewer.headers)
    assert all(p["id"] != post["id"] for p in campus_feed.json())


async def test_soft_deleted_comment_excluded_from_comment_count(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A soft-deleted comment (deleted_at set) must not count toward comment_count.

    No API route soft-deletes a comment yet, so this sets deleted_at directly in
    the DB (same technique tests/test_pagination.py uses to control created_at).
    """
    setup = await make_chapter_with("president")
    post = await _create_post(client, setup.chapter_id, setup.president.headers, "comment test")

    live = await client.post(
        f"/posts/{post['id']}/comments", json={"body": "stays"}, headers=setup.president.headers
    )
    assert live.status_code == 201, live.text
    doomed = await client.post(
        f"/posts/{post['id']}/comments",
        json={"body": "gets soft-deleted"},
        headers=setup.president.headers,
    )
    assert doomed.status_code == 201, doomed.text

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE post_comments SET deleted_at = now() WHERE id = :id"),
            {"id": uuid.UUID(doomed.json()["id"])},
        )
        await session.commit()

    listing = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.president.headers
    )
    assert listing.status_code == 200, listing.text
    row = next(p for p in listing.json() if p["id"] == post["id"])
    assert row["comment_count"] == 1, "soft-deleted comment must not count"
