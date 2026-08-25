"""Blocking hides a person's COMMENTS too, not just their posts (board card c109).

The block filter existed on the post query and on chirps, and not on comments — so
blocking a harasser hid their posts and left their comments underneath everyone else's.
The app promises otherwise in words: the confirm dialog says "You won't see posts from
this person again". Same family as c76, where /terms claimed removal powers the server
did not have.

The count matters as much as the list: a card that reads "2 comments" and opens to show
one is the same lie in a smaller font.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


async def _chapter_with_two_members(client: AsyncClient, make_chapter_with, make_user):
    setup = await make_chapter_with("president")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    other = await make_user("The Blocked One")
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=other.headers
    )
    assert joined.status_code == 201, joined.text
    return setup, other


async def test_blocking_hides_their_comments_and_fixes_the_count(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup, other = await _chapter_with_two_members(client, make_chapter_with, make_user)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "a post with comments"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    for author, text in ((setup.president, "mine"), (other, "theirs")):
        c = await client.post(
            f"/posts/{post_id}/comments", json={"body": text}, headers=author.headers
        )
        assert c.status_code == 201, c.text

    before = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    assert len(before.json()) == 2, before.text

    blocked = await client.post(
        "/moderation/blocks",
        json={"blocked_id": other.id},
        headers=setup.president.headers,
    )
    assert blocked.status_code == 201, blocked.text

    after = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    bodies = [c["body"] for c in after.json()]
    assert bodies == ["mine"], f"the blocked author's comment must be gone; got {bodies}"

    # The count on the feed card has to agree with what opening it shows.
    feed = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.president.headers
    )
    row = [p for p in feed.json() if p["id"] == post_id][0]
    assert row["comment_count"] == 1, (
        f"comment_count must match the filtered list, got {row['comment_count']}"
    )


async def test_blocking_is_one_directional_for_comments(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The blocker stops seeing them. They do not stop seeing the blocker — matching
    how the post filter already behaves, since the ON clause pins blocker_id to the
    caller."""
    setup, other = await _chapter_with_two_members(client, make_chapter_with, make_user)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "post"},
        headers=setup.president.headers,
    )
    post_id = post.json()["id"]
    for author, text in ((setup.president, "from president"), (other, "from other")):
        assert (
            await client.post(
                f"/posts/{post_id}/comments", json={"body": text}, headers=author.headers
            )
        ).status_code == 201

    assert (
        await client.post(
            "/moderation/blocks",
            json={"blocked_id": other.id},
            headers=setup.president.headers,
        )
    ).status_code == 201

    theirs = await client.get(f"/posts/{post_id}/comments", headers=other.headers)
    assert len(theirs.json()) == 2, "the blocked user still sees the whole thread"


async def test_a_block_does_not_hide_comments_from_an_uninvolved_reader(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Guards against the filter fanning out — the ON clause must pin blocker_id to the
    caller, or one person's block would censor the thread for everyone."""
    setup, other = await _chapter_with_two_members(client, make_chapter_with, make_user)
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    bystander = await make_user("Uninvolved")
    assert (
        await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=bystander.headers,
        )
    ).status_code == 201

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "post"},
        headers=setup.president.headers,
    )
    post_id = post.json()["id"]
    assert (
        await client.post(
            f"/posts/{post_id}/comments", json={"body": "theirs"}, headers=other.headers
        )
    ).status_code == 201

    assert (
        await client.post(
            "/moderation/blocks",
            json={"blocked_id": other.id},
            headers=setup.president.headers,
        )
    ).status_code == 201

    seen = await client.get(f"/posts/{post_id}/comments", headers=bystander.headers)
    assert [c["body"] for c in seen.json()] == ["theirs"], (
        "one reader's block must not censor the thread for anyone else"
    )
