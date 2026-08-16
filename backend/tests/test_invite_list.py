"""Listing a chapter's invites, so revocation can actually be reached (board c111).

c105 shipped POST /chapters/{id}/invites/revoke, which takes the code string. That
covers a president who still has the code in front of them and nobody else — and
minting was the only place a code was ever returned, so a code posted three weeks
ago and forwarded twice could not be revoked at all. The route existed and was
unreachable, which reads as complete and is not.

These tests cover the properties that make the list safe rather than the shape of
the JSON: it is e-board only, it never crosses chapters, and it shows dead codes
instead of hiding them.
"""

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


async def _mint(client: AsyncClient, setup, **body) -> dict:
    payload = {"role": "member"}
    payload.update(body)
    response = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json=payload,
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_an_eboard_member_sees_every_code_the_chapter_minted(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The whole point: a code you are not holding is now findable."""
    setup = await make_chapter_with("president")
    minted = [await _mint(client, setup) for _ in range(3)]

    response = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
    )
    assert response.status_code == 200, response.text
    listed = {row["code"] for row in response.json()}
    assert listed == {invite["code"] for invite in minted}


async def test_the_list_carries_what_you_need_to_decide(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A president deciding whether to kill a code needs its budget, not just its id."""
    setup = await make_chapter_with("president")
    invite = await _mint(client, setup, max_uses=5)
    joined = await client.post(
        "/chapters/join",
        json={"code": invite["code"]},
        headers=(await make_user("A Joiner")).headers,
    )
    assert joined.status_code == 201, joined.text

    response = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
    )
    row = next(r for r in response.json() if r["code"] == invite["code"])
    assert row["uses"] == 1
    assert row["max_uses"] == 5
    assert row["expires_at"] is not None
    assert row["revoked_at"] is None
    assert row["role"] == "member"


async def test_revoked_and_spent_codes_are_still_listed(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Hiding dead codes would make a leaked one look like it was never minted.

    "Is the code that is going around still live" is the question this screen
    exists to answer, and it cannot be answered by a list that silently drops it.
    """
    setup = await make_chapter_with("president")
    revoked = await _mint(client, setup)
    spent = await _mint(client, setup, max_uses=1)

    killed = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": revoked["code"]},
        headers=setup.president.headers,
    )
    assert killed.status_code == 200, killed.text
    used = await client.post(
        "/chapters/join",
        json={"code": spent["code"]},
        headers=(await make_user("Spender")).headers,
    )
    assert used.status_code == 201, used.text

    rows = {r["code"]: r for r in (await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
    )).json()}
    assert rows[revoked["code"]]["revoked_at"] is not None
    assert rows[spent["code"]]["uses"] == rows[spent["code"]]["max_uses"]


async def test_a_plain_member_cannot_list_invites(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Codes are credentials. Reading them all is an e-board action, like minting."""
    setup = await make_chapter_with("member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_one_chapter_cannot_list_another_chapters_invites(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """require_role proves you are e-board SOMEWHERE, which is not the same as here.

    The same hole the c105 revoke route had to close by hand. Without the scope, a
    president of any chapter could read every other chapter's live codes, which
    would turn this endpoint into a better attack than the one c105 fixed.
    """
    mine = await make_chapter_with("president")
    theirs = await make_chapter_with("president")
    await _mint(client, theirs)

    response = await client.get(
        f"/chapters/{theirs.chapter_id}/invites", headers=mine.president.headers
    )
    assert response.status_code == 403, response.text


async def test_a_chapter_with_no_invites_returns_an_empty_list(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Empty is a real state — the UI renders it, so it must not be a 404."""
    setup = await make_chapter_with("president")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == []
