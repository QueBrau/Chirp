"""Chapter-less students can post to their campus (board card c71).

The product decision behind this: Chirp is for ALL students and greek chapters are
an optional thing you join (board Decisions log, Aug 11). Until c71 the only
create-post route required an active chapter membership, so a student in no org
could read the campus feed and never contribute to it.

What these tests pin down, in the order the privacy argument runs:

1. a chapter-less student CAN create a campus post, and it reaches the feed;
2. that student CANNOT produce an 'org' post by any route, including by smuggling
   an audience field into the campus route's body;
3. the database itself refuses an org post with no chapter, so (2) does not rest on
   the router being careful;
4. and the original guarantee is intact - an 'org' post still never appears on the
   campus feed, which matters more now that org posts carry a campus_id of their own
   and would leak the moment the audience filter was dropped.

Also covers the like/comment authorization that c71 had to fix to ship: interaction
on a campus post is scoped by campus, not by membership of the authoring chapter.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ApiUser, MakeChapterWith, MakeUser


async def _make_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Campus User"
) -> ApiUser:
    """Bootstrap a user pinned to `campus_id` and belonging to NO chapter.

    Same helper as test_feed_audience.py; duplicated rather than shared because
    every test module that needs it already keeps its own copy.
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
            "campus_id": campus_id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)


async def _campus_id_of(client: AsyncClient, chapter_id: str, headers: dict[str, str]) -> str:
    response = await client.get(f"/chapters/{chapter_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["campus_id"]


# ---------------------------------------------------------------------------
# The feature: a student with no org can post
# ---------------------------------------------------------------------------


async def test_chapterless_student_can_post_and_it_reaches_the_campus_feed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The c71 headline: no membership, still able to post, and the post is visible.

    Asserts the feed too, not just the 201. A post that saves and never appears is
    the exact failure the chapters JOIN in the campus feed would have produced.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    loner = await _make_campus_user(client, campus_id, "Unaffiliated Student")

    created = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "anyone going to the career fair"},
        headers=loner.headers,
    )
    assert created.status_code == 201, created.text
    post = created.json()
    assert post["chapter_id"] is None, "a chapter-less post must not claim an org"
    assert post["campus_id"] == campus_id
    assert post["audience"] == "campus"

    feed = await client.get(f"/campuses/{campus_id}/feed", headers=loner.headers)
    assert feed.status_code == 200, feed.text
    assert post["id"] in [p["id"] for p in feed.json()], (
        "the chapter-less post must appear on the campus feed it was posted to"
    )


async def test_chapterless_student_can_like_and_comment_on_a_campus_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Posting is useless if nobody can engage with the result.

    Interaction is authorized through the post's chapter, so before c71 a
    chapter-less post had no chapter to be a member of and every like on it - even
    the author's own - was a 403.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    author = await _make_campus_user(client, campus_id, "Loner Author")
    reader = await _make_campus_user(client, campus_id, "Loner Reader")

    created = await client.post(
        f"/campuses/{campus_id}/posts", json={"body": "free pizza"}, headers=author.headers
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    liked = await client.put(f"/posts/{post_id}/likes", headers=reader.headers)
    assert liked.status_code == 200, liked.text
    commented = await client.post(
        f"/posts/{post_id}/comments", json={"body": "where"}, headers=reader.headers
    )
    assert commented.status_code == 201, commented.text


async def test_campus_post_is_likeable_by_a_student_from_another_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Pre-existing bug, fixed as part of c71.

    Likes authorized through the post's chapter, so a campus post from chapter A was
    un-likeable by everyone on campus who was not in chapter A - which is nearly
    everyone the campus feed shows it to.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    outsider = await _make_campus_user(client, campus_id, "Other Chapter Student")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "rush week", "audience": "campus"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    liked = await client.put(f"/posts/{created.json()['id']}/likes", headers=outsider.headers)
    assert liked.status_code == 200, liked.text


# ---------------------------------------------------------------------------
# The limits: 'org' stays unreachable without a membership
# ---------------------------------------------------------------------------


async def test_chapterless_student_cannot_post_into_a_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The membership-gated route is unchanged: no membership, no chapter post."""
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    loner = await _make_campus_user(client, campus_id, "Outsider")

    response = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "let me in", "audience": "org"},
        headers=loner.headers,
    )
    assert response.status_code == 403, response.text


async def test_campus_route_cannot_be_talked_into_making_an_org_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """An 'audience' in the body must not change what the campus route produces.

    CampusPostCreate has no audience field, so the value is ignored rather than
    honoured. Asserted on the stored post, not on the status code: a 201 that
    quietly created an org post is the failure being ruled out.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    loner = await _make_campus_user(client, campus_id, "Sneaky Student")

    created = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "smuggled", "audience": "org", "chapter_id": setup.chapter_id},
        headers=loner.headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["audience"] == "campus"
    assert created.json()["chapter_id"] is None


async def test_cannot_post_to_a_campus_you_do_not_belong_to(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Campus scoping on the write path matches the read path's _require_campus_user."""
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    stranger = await make_user("No Campus")  # campus_id is NULL for a plain make_user

    response = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "not my campus"},
        headers=stranger.headers,
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_your_campus"}


async def test_database_refuses_an_org_post_with_no_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """ck_posts_org_requires_chapter, proven by going around the router entirely.

    'org' means "private to this chapter". A row claiming org with no chapter is not
    a looser permission but an unscopeable one: every org read path filters on
    chapter_id, and NULL matches nothing while the row still exists. The constraint
    is what makes chapter_id's new nullability safe, so it is tested directly rather
    than trusted because the routers currently happen to be careful.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        with pytest.raises(IntegrityError) as excinfo:
            await session.execute(
                text(
                    "INSERT INTO posts (chapter_id, campus_id, author_id, body, audience)"
                    " VALUES (NULL, :campus_id, :author_id, 'orphan org post', 'org')"
                ),
                {
                    "campus_id": uuid.UUID(campus_id),
                    "author_id": uuid.UUID(setup.president.id),
                },
            )
            await session.commit()
        await session.rollback()

    assert "ck_posts_org_requires_chapter" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The guarantee that must survive all of the above
# ---------------------------------------------------------------------------


async def test_org_post_still_never_reaches_the_campus_feed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The Aug 14 privacy promise, re-pinned against the c71 schema.

    This duplicates test_feed_audience.py's coverage on purpose. Org posts now carry
    a campus_id of their own, and the campus feed now filters on that column instead
    of joining chapters, so the audience filter is the ONLY thing standing between an
    org post and the campus feed. Deleting it would leave a query that still looks
    correct and leaks every private chapter post on the campus.
    """
    setup = await make_chapter_with("president")
    campus_id = await _campus_id_of(client, setup.chapter_id, setup.president.headers)
    viewer = await _make_campus_user(client, campus_id, "Campus Viewer")

    org_post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "chapter business only", "audience": "org"},
        headers=setup.president.headers,
    )
    assert org_post.status_code == 201, org_post.text
    # The org post really is on this campus - so its absence below is the audience
    # filter working, not the post being scoped away to somewhere else.
    assert org_post.json()["campus_id"] == campus_id

    feed = await client.get(f"/campuses/{campus_id}/feed", headers=viewer.headers)
    assert feed.status_code == 200, feed.text
    assert org_post.json()["id"] not in [p["id"] for p in feed.json()], (
        "an 'org' post must NEVER appear on the campus feed"
    )
