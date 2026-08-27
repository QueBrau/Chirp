"""The campus gate: a chapter-derived campus is NOT a verified campus (c88).

THIS FILE IS THE TRIPWIRE. Every test here builds the exact user the bypass produced —
someone with a campus_id but no .edu verification, which since c96 is what redeeming a
chapter invite makes — and asserts they are refused. If any of these start passing a
403 back as a 200, the gate has been removed from that route.

The bypass they protect against, for whoever reads this after it breaks: the only
campus check used to be `user.campus_id != campus_id`. That was safe while nothing
wrote campus_id. c96 made an invite redemption write it, and per c105 an invite code is
an unlimited-use bearer token with no revocation — so one forwarded code granted read
AND write on a campus's Chirp board and campus feed with no email of any kind.

DO NOT "fix" a failure here by relaxing the assertion or by letting a non-null
campus_id satisfy the gate. That is the bypass, restored.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import MakeCampus, MakeChapterWith, MakeUser, set_campus

UNVERIFIED = "campus_unverified"
WRONG_CAMPUS = "not_your_campus"


@pytest.fixture
async def unverified(make_user: MakeUser, make_campus: MakeCampus):
    """A user with a campus and NO verification — the c96 invite-redemption shape."""
    user = await make_user()
    campus_id = await make_campus()
    await set_campus(user.id, campus_id, verified=False)
    return user, campus_id


@pytest.fixture
async def verified(make_user: MakeUser, make_campus: MakeCampus):
    """A user who has proved an .edu at their campus."""
    user = await make_user()
    campus_id = await make_campus()
    await set_campus(user.id, campus_id, verified=True)
    return user, campus_id


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_campus_feed_refuses_an_unverified_user(client: AsyncClient, unverified) -> None:
    """GET /campuses/{id}/feed — the surface the whole ruling is about."""
    user, campus_id = unverified

    response = await client.get(f"/campuses/{campus_id}/feed", headers=user.headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_chirp_list_refuses_an_unverified_user(client: AsyncClient, unverified) -> None:
    """GET /campuses/{id}/chirps — the other half of the ruling."""
    user, campus_id = unverified

    response = await client.get(f"/campuses/{campus_id}/chirps", headers=user.headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


# ---------------------------------------------------------------------------
# Writes — the same hole facing the other direction
# ---------------------------------------------------------------------------


async def test_posting_a_chirp_refuses_an_unverified_user(client: AsyncClient, unverified) -> None:
    """POST /campuses/{id}/chirps. Gating the read alone would let an unverified user
    broadcast to a board they cannot read."""
    user, campus_id = unverified

    response = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "hello"}, headers=user.headers
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_voting_refuses_an_unverified_user(
    client: AsyncClient, unverified, verified
) -> None:
    """PUT /chirps/{id}/vote had its OWN inline copy of the old check that the shared
    dependency never covered — a third copy, in the file that already had two."""
    author, campus_id = verified
    create = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "votable"}, headers=author.headers
    )
    assert create.status_code == 201, create.text
    chirp_id = create.json()["id"]

    voter, _ = unverified
    await set_campus(voter.id, campus_id, verified=False)

    response = await client.put(
        f"/chirps/{chirp_id}/vote", json={"value": 1}, headers=voter.headers
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_campus_audience_post_refuses_an_unverified_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """POST /chapters/{id}/posts with audience='campus'.

    THE WRITE-SIDE HOLE, and the one least likely to be found by reading the campus
    routes: the path is chapter-scoped, so it never touched the campus dependency and
    read as an org endpoint — but audience='campus' publishes to the campus feed. A
    campus-wide write wearing a chapter URL.
    """
    setup = await make_chapter_with("member")

    response = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "campus wide", "audience": "campus"},
        headers=setup.member.headers,
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_org_post_still_works_for_an_unverified_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The other half of the ruling, and the reason the gate is not simply 'block
    chapter members': chapter membership DOES grant chapter content with no email.
    If this breaks, the gate is too wide and alpha loses org posting."""
    setup = await make_chapter_with("member")

    response = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "org only", "audience": "org"},
        headers=setup.member.headers,
    )

    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# The gate must not swallow the other 403
# ---------------------------------------------------------------------------


async def test_wrong_campus_is_still_distinguishable_from_unverified(
    client: AsyncClient, verified, make_campus: MakeCampus
) -> None:
    """A verified user asking for SOMEONE ELSE'S campus gets not_your_campus, not
    campus_unverified. c90's screen branches on these: one is 'verify your .edu' with
    a form, the other has no action behind it at all."""
    user, _ = verified
    other_campus = await make_campus()

    response = await client.get(f"/campuses/{other_campus}/feed", headers=user.headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == WRONG_CAMPUS


async def test_a_user_with_no_campus_at_all_is_refused(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """campus_id NULL never satisfies the comparison — the state every prod user is in
    today, and the reason both tabs are dark rather than open."""
    user = await make_user()
    campus_id = await make_campus()

    response = await client.get(f"/campuses/{campus_id}/feed", headers=user.headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == WRONG_CAMPUS


async def test_a_verified_user_can_still_use_their_campus(
    client: AsyncClient, verified
) -> None:
    """The gate must refuse the right people and only them. Without this, a gate that
    403s everyone would pass every other test in this file."""
    user, campus_id = verified

    feed = await client.get(f"/campuses/{campus_id}/feed", headers=user.headers)
    chirps = await client.get(f"/campuses/{campus_id}/chirps", headers=user.headers)

    assert feed.status_code == 200, feed.text
    assert chirps.status_code == 200, chirps.text


async def test_deleting_your_own_chirp_survives_a_lapsed_verification(
    client: AsyncClient, verified
) -> None:
    """Author deletion is deliberately NOT campus-gated. Gating it would trap content:
    a student whose yearly re-check lapsed could no longer retract their own post."""
    user, campus_id = verified
    create = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "mine"}, headers=user.headers
    )
    assert create.status_code == 201, create.text
    chirp_id = create.json()["id"]

    await set_campus(user.id, campus_id, verified=False)

    response = await client.delete(f"/chirps/{chirp_id}", headers=user.headers)

    assert response.status_code == 204, response.text


# ---------------------------------------------------------------------------
# c108 — moderating campus content requires a verified .edu, not just the role
# ---------------------------------------------------------------------------


async def _open_report_on_a_chirp(client: AsyncClient, reporter, chirp_id: str) -> str:
    response = await client.post(
        "/moderation/reports",
        json={"target_type": "chirp", "target_id": chirp_id, "reason": "test"},
        headers=reporter.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_an_unverified_officer_cannot_moderate_campus_content(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """THE c108 TRIPWIRE, and it guards all THREE moderation endpoints at once because
    they share one function.

    Jose ruled Aug 16 that moderating campus content requires a verified .edu — stricter
    than the officer-role-is-enough recommendation on that card. So a president who has
    never proved an .edu keeps every chapter power and loses the campus Chirp board.

    If this starts passing 204s back, the verification line came out of
    _require_eboard_for_campus and all three endpoints regressed together.
    """
    setup = await make_chapter_with("member")
    # A verified author to create the chirp the officer will try to remove.
    from tests.conftest import verify_campus

    await verify_campus(setup.member.id)
    campus = await client.get(f"/chapters/{setup.chapter_id}", headers=setup.president.headers)
    assert campus.status_code == 200, campus.text
    campus_id = campus.json()["campus_id"]

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "moderate me"}, headers=setup.member.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]
    report_id = await _open_report_on_a_chirp(client, setup.member, chirp_id)

    # The president is active e-board on this campus but has never verified.
    remove_chirp = await client.post(
        f"/moderation/chirps/{chirp_id}/remove",
        json={"reason": "spam"},
        headers=setup.president.headers,
    )
    resolve = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "no action"},
        headers=setup.president.headers,
    )

    for label, response in (("remove_chirp", remove_chirp), ("resolve_chirp_report", resolve)):
        assert response.status_code == 403, f"{label}: {response.text}"
        assert response.json()["detail"] == UNVERIFIED, f"{label}: {response.text}"


async def test_an_unverified_officer_can_still_moderate_their_own_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """THE OTHER HALF OF c108, and the one that keeps it from being an over-reach.

    _require_eboard_for_campus gates BOTH tiers - chirps, which are campus-wide, and posts
    and comments, which are chapter content. Putting the .edu check in unconditionally
    would lock an unverified president out of moderating their OWN chapter's posts,
    which is the exact opposite of the ruling that chapter membership grants chapter
    content in full with no email at all.

    So: an unverified officer removes a member's org post normally. If this starts
    403ing, campus_content=True leaked onto a chapter-content path.
    """
    setup = await make_chapter_with("member")
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "org post", "audience": "org"},
        headers=setup.member.headers,
    )
    assert post.status_code == 201, post.text

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": "spam"},
        headers=setup.president.headers,
    )

    assert response.status_code == 204, response.text


async def test_a_verified_officer_can_still_moderate(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c108 must refuse the right people and only them — without this, a rule that
    refused every moderator would pass the test above."""
    from tests.conftest import verify_campus

    setup = await make_chapter_with("member")
    await verify_campus(setup.member.id)
    await verify_campus(setup.president.id)
    campus = await client.get(f"/chapters/{setup.chapter_id}", headers=setup.president.headers)
    campus_id = campus.json()["campus_id"]

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "moderate me"}, headers=setup.member.headers
    )
    assert chirp.status_code == 201, chirp.text

    response = await client.post(
        f"/moderation/chirps/{chirp.json()['id']}/remove",
        json={"reason": "spam"},
        headers=setup.president.headers,
    )

    assert response.status_code == 204, response.text
