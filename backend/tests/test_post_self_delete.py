"""An author can delete their own post without naming a chapter (board card c84).

The existing route is DELETE /chapters/{chapter_id}/posts/{post_id}, which matches on
post.chapter_id == the path chapter. That is unsatisfiable for a post with no chapter:
there is nothing to put in the URL. On main today posts.chapter_id is NOT NULL so the
broken state is unreachable — but q/campus-posts (c71) changes it to nullable and still
ships only the chapter-scoped delete, so these tests exist to make the fix land BEFORE
the bug rather than alongside it.

/privacy is live and promises deletion, and c69's purge job is built on the same
promise, so a delete the author cannot reach makes both untrue.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


async def _post_as(client: AsyncClient, setup, user) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "mine to delete"},
        headers=user.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_author_can_delete_their_own_post_without_a_chapter_in_the_url(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    post_id = await _post_as(client, setup, setup.member)

    deleted = await client.delete(f"/posts/{post_id}", headers=setup.member.headers)
    assert deleted.status_code == 204, deleted.text

    feed = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert post_id not in [p["id"] for p in feed.json()], "a soft-deleted post must leave the feed"


async def test_a_stranger_cannot_delete_someone_elses_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    other = await make_chapter_with("member")
    post_id = await _post_as(client, setup, setup.member)

    attempt = await client.delete(f"/posts/{post_id}", headers=other.member.headers)
    assert attempt.status_code == 403, attempt.text

    # Still there, checked from the owning side rather than by trusting the status code.
    feed = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert post_id in [p["id"] for p in feed.json()]


async def test_a_plain_member_cannot_delete_another_members_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Same chapter is not authorization — only the author or the president."""
    setup = await make_chapter_with("president")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    member = await make_user("Ordinary Member")
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=member.headers
    )
    assert joined.status_code == 201, joined.text

    post_id = await _post_as(client, setup, setup.president)
    attempt = await client.delete(f"/posts/{post_id}", headers=member.headers)
    assert attempt.status_code == 403, attempt.text


async def test_president_can_delete_a_members_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Parity with the chapter-scoped route — a president keeps the same power here."""
    setup = await make_chapter_with("member")
    post_id = await _post_as(client, setup, setup.member)

    deleted = await client.delete(f"/posts/{post_id}", headers=setup.president.headers)
    assert deleted.status_code == 204, deleted.text


async def test_deleting_twice_is_404_not_a_silent_success(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    post_id = await _post_as(client, setup, setup.member)

    first = await client.delete(f"/posts/{post_id}", headers=setup.member.headers)
    assert first.status_code == 204, first.text
    second = await client.delete(f"/posts/{post_id}", headers=setup.member.headers)
    assert second.status_code == 404, second.text


async def test_unknown_post_is_404(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    attempt = await client.delete(
        "/posts/00000000-0000-0000-0000-000000000000", headers=setup.member.headers
    )
    assert attempt.status_code == 404, attempt.text
