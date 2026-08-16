"""Campus is derived from the chapter you join (board card c96).

Before this, nothing anywhere wrote users.campus_id — c85 removed the client-supplied
value at bootstrap and named c86's `.edu` redemption as the only writer, then c86 was
deferred. Every user sat at NULL forever, which dead-ends Home's Campus tab and the
whole Yak tab and makes gate c71 unreachable.

The point of these tests is not "campus gets set". It is the two guarantees that make
setting it safe: the value comes off the CHAPTER (never a request body, so c85 stays
closed), and it only ever fills a NULL (so a later .edu verification, which is the
stronger claim, can never be clobbered by an invite redemption).
"""

import uuid

from httpx import AsyncClient

from tests.conftest import MakeCampus, MakeChapterWith, MakeUser, set_campus


async def _campus_of(client: AsyncClient, user) -> str | None:
    response = await client.get("/auth/me", headers=user.headers)
    assert response.status_code == 200, response.text
    return response.json()["user"]["campus_id"]


async def test_joining_a_chapter_grants_that_chapters_campus(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The whole point of c96: an invited member can now reach campus content."""
    setup = await make_chapter_with("member")

    chapter = await client.get(
        f"/chapters/{setup.chapter_id}", headers=setup.president.headers
    )
    assert chapter.status_code == 200, chapter.text

    assert await _campus_of(client, setup.member) == chapter.json()["campus_id"]


async def test_founding_president_gets_the_campus_they_created_on(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """POST /chapters is the other membership-creating path and must behave the same."""
    setup = await make_chapter_with("president")

    chapter = await client.get(
        f"/chapters/{setup.chapter_id}", headers=setup.president.headers
    )
    assert chapter.status_code == 200, chapter.text

    assert await _campus_of(client, setup.president) == chapter.json()["campus_id"]


async def test_join_never_overwrites_a_campus_the_user_already_has(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    make_campus: MakeCampus,
) -> None:
    """The guarantee that keeps c86 meaningful.

    A verified `.edu` campus is a stronger claim than "someone sent me an invite
    code". If redeeming an invite could overwrite it, a chapter on campus B could
    silently move a student who verified at campus A — which is exactly the kind of
    client-influenced campus reassignment c85 exists to prevent.
    """
    setup = await make_chapter_with("president")
    already_verified_campus = await make_campus()

    joiner = await make_user("Already Verified")
    # Stands in for what c86's .edu redemption will do; see conftest.set_campus.
    await set_campus(joiner.id, already_verified_campus)

    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join",
        json={"code": invite.json()["code"]},
        headers=joiner.headers,
    )
    assert joined.status_code == 201, joined.text

    assert await _campus_of(client, joiner) == already_verified_campus


async def test_bootstrap_still_refuses_a_client_supplied_campus(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """c85's guarantee, re-asserted here because c96 is the card most likely to erode it.

    c96 makes campus reachable again, which removes the pressure that would otherwise
    tempt someone into 'just let bootstrap take it'. This test fails loudly if that
    ever happens — see also c101, where an unmerged branch still does exactly this.
    """
    campus_id = await make_campus()
    uid = f"uid-{uuid.uuid4().hex}"

    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": f"{uid}@example.edu",
            "display_name": "Campus Claimer",
            "account_type": "non_greek",
            "campus_id": campus_id,
        },
        headers={"X-Debug-Firebase-Uid": uid},
    )
    assert response.status_code == 201, response.text
    assert response.json()["campus_id"] is None, (
        "bootstrap must ignore a client-supplied campus_id (c85); a new account has "
        "proved nothing yet"
    )
