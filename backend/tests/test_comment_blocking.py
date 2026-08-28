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


# ---------------------------------------------------------------------------
# c228: the author identity the comments sheet renders. The block filter above and
# these fields come out of the SAME query, so they are pinned in the same file - a
# rewrite of list_comments' select that adds the join and drops the anti-join (or
# vice versa) fails here rather than in whichever suite happens to run first.
# ---------------------------------------------------------------------------


async def test_comments_carry_their_author_display_identity(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A comment names its author.

    There is no route that turns a user id into a person, so without these fields the
    only thing a client could render for each comment is the raw UUID.
    """
    setup, other = await _chapter_with_two_members(client, make_chapter_with, make_user)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "post"},
        headers=setup.president.headers,
    )
    post_id = post.json()["id"]
    assert (
        await client.post(
            f"/posts/{post_id}/comments", json={"body": "hello"}, headers=other.headers
        )
    ).status_code == 201

    listed = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    assert listed.status_code == 200, listed.text
    (comment,) = listed.json()
    assert comment["display_name"] == "The Blocked One", comment
    assert comment["author_id"] == other.id
    # Nullable in the schema and null for a user who never set one - present as a key
    # regardless, so the client never has to distinguish "absent" from "none".
    assert "avatar_url" in comment
    assert comment["avatar_url"] is None


async def test_creating_a_comment_returns_the_same_shape_the_list_does(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """POST's response is the row the client appends to the open thread.

    If it came back without the identity fields the newly-sent comment would be the one
    entry in the sheet with no name on it, so this is the same bug as the list's, one
    comment wide.
    """
    setup, other = await _chapter_with_two_members(client, make_chapter_with, make_user)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "post"},
        headers=setup.president.headers,
    )
    post_id = post.json()["id"]

    created = await client.post(
        f"/posts/{post_id}/comments", json={"body": "just sent"}, headers=other.headers
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["display_name"] == "The Blocked One", body

    listed = await client.get(f"/posts/{post_id}/comments", headers=other.headers)
    (from_list,) = listed.json()
    assert from_list == body, "POST and GET must agree field for field"


async def test_the_author_join_does_not_leak_blocked_authors_back_in(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The c109 anti-join and the c228 author join have to hold at the same time.

    Adding a second join to this query is exactly the kind of edit that quietly changes
    which rows survive, and the failure is invisible from the count alone: this asserts
    the surviving row's NAME, so a filter that drops the wrong author fails loudly.
    """
    setup, other = await _chapter_with_two_members(client, make_chapter_with, make_user)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "post"},
        headers=setup.president.headers,
    )
    post_id = post.json()["id"]
    for author, text in ((setup.president, "mine"), (other, "theirs")):
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

    after = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    rows = after.json()
    assert [c["body"] for c in rows] == ["mine"], rows
    assert [c["display_name"] for c in rows] == ["Chapter President"], rows

    # And the chip's number still agrees with the sheet's contents (c109). The sheet
    # renders `len(rows)`, so a disagreement here is a visible one.
    feed = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.president.headers
    )
    row = [p for p in feed.json() if p["id"] == post_id][0]
    assert row["comment_count"] == len(rows), (
        f"comment_count {row['comment_count']} must equal the {len(rows)} comment(s) listed"
    )
