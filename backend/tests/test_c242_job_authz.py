"""c242: the job board's gate is a MEMBERSHIP, and its read is chapter-scoped.

POST /jobs used to compute eligibility as

    (active membership with an e-board/alumni role anywhere) or user.account_type == "alumni"

and account_type is whatever the client sent to POST /auth/bootstrap. So the
second half of that `or` was "the caller says so". Anyone could tap "Alumni" on
the account-type screen and post — and GET /jobs then handed every
`chapter_id IS NULL` row to EVERY authenticated caller network-wide, each row
carrying an apply_url the app opens with Linking.openURL. Self-service phishing
distribution into the whole user base.

These tests pin both halves of the fix. The posting tests are written so that a
regression cannot hide behind a passing suite: the eligible callers here all have
account_type "greek" (make_chapter_with's default), so it is provably the
membership row doing the work, and the refused callers all declare "alumni", so
re-adding the account_type branch turns them green and fails these.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import ApiUser, MakeChapterWith, MakeUser

JOB = {
    "title": "Summer Analyst",
    "company": "Northgate Capital",
    "location": "Chicago, IL",
    "description": "Paid summer internship on the private credit team.",
}


def _job(chapter_id: str | None) -> dict[str, object]:
    return {"chapter_id": chapter_id, **JOB}


async def _join(
    client: AsyncClient, chapter_id: str, president: ApiUser, role: str, user: ApiUser
) -> None:
    """Add an EXISTING user to an existing chapter with the given role.

    make_chapter_with always mints a fresh user for the role it is asked for, so
    it cannot express "this same person is also in that chapter" — which is the
    exact shape the cross-chapter tests below need.
    """
    invite = await client.post(
        f"/chapters/{chapter_id}/invites", json={"role": role}, headers=president.headers
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=user.headers
    )
    assert joined.status_code == 201, joined.text


# ---- POST /jobs: who may post ----


async def test_self_declared_alumni_with_no_membership_cannot_post(
    client: AsyncClient, make_user: MakeUser, make_chapter_with: MakeChapterWith
) -> None:
    """The c242 attacker exactly: signs up as "alumni", belongs to nothing.

    Refused for a network-wide post and for someone else's chapter alike — the
    two shapes the old code let through and let through respectively.
    """
    attacker = await make_user("Walk In", account_type="alumni")
    someone_elses_chapter = (await make_chapter_with("president")).chapter_id

    network = await client.post("/jobs", json=_job(None), headers=attacker.headers)
    assert network.status_code == 403, network.text
    assert network.json()["detail"] == "alumni_or_eboard_only"

    targeted = await client.post(
        "/jobs", json=_job(someone_elses_chapter), headers=attacker.headers
    )
    assert targeted.status_code == 403, targeted.text
    assert targeted.json()["detail"] == "not_a_member"


async def test_self_declaring_alumni_adds_nothing_to_a_plain_member(
    client: AsyncClient, make_user: MakeUser, make_chapter_with: MakeChapterWith
) -> None:
    """A real member who ALSO ticked "alumni" at signup is still just a member.

    The harder half of the hole: this caller does hold a membership, so a fix that
    only checked "has any membership" would let them through. Role is what counts.
    """
    setup = await make_chapter_with("president")
    liar = await make_user("Plain Member", account_type="alumni")
    await _join(client, setup.chapter_id, setup.president, "member", liar)

    for chapter_id in (setup.chapter_id, None):
        resp = await client.post("/jobs", json=_job(chapter_id), headers=liar.headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "alumni_or_eboard_only"


async def test_genuine_alumni_member_can_still_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The legitimate poster the board exists for: role "alumni" in a real chapter.

    Their account_type is "greek" (make_chapter_with's default) and they post
    anyway, which is the point — the membership is doing the work.
    """
    setup = await make_chapter_with("alumni")

    targeted = await client.post(
        "/jobs", json=_job(setup.chapter_id), headers=setup.member.headers
    )
    assert targeted.status_code == 201, targeted.text

    network = await client.post("/jobs", json=_job(None), headers=setup.member.headers)
    assert network.status_code == 201, network.text


async def test_eboard_member_can_still_post(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """E-board is the other half of the intended audience ("Alumni and e-board")."""
    setup = await make_chapter_with("historian")

    by_officer = await client.post(
        "/jobs", json=_job(setup.chapter_id), headers=setup.member.headers
    )
    assert by_officer.status_code == 201, by_officer.text

    by_president = await client.post(
        "/jobs", json=_job(setup.chapter_id), headers=setup.president.headers
    )
    assert by_president.status_code == 201, by_president.text


async def test_plain_member_cannot_post_to_their_own_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Being in the chapter was never enough, and still is not."""
    setup = await make_chapter_with("member")
    resp = await client.post(
        "/jobs", json=_job(setup.chapter_id), headers=setup.member.headers
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "alumni_or_eboard_only"


async def test_alumni_of_one_chapter_cannot_post_to_a_chapter_they_are_not_in(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """§8.4 spirit: a qualifying role elsewhere is not a key to this chapter."""
    alumni_of_a = (await make_chapter_with("alumni")).member
    chapter_b = (await make_chapter_with("president")).chapter_id

    resp = await client.post("/jobs", json=_job(chapter_b), headers=alumni_of_a.headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "not_a_member"


async def test_qualifying_role_must_be_held_in_the_target_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Alumni of A, plain member of B, posting to B: refused.

    This is the hole the old two-question check left open even ignoring
    account_type — it asked "eligible role ANYWHERE?" and "member of THIS
    chapter?" separately, and this caller answers yes to both while being nobody's
    alumnus in B. Nothing in the product says an alumnus of one chapter may put
    listings on another chapter's board, so it is refused.
    """
    setup_a = await make_chapter_with("alumni")
    setup_b = await make_chapter_with("president")
    await _join(client, setup_b.chapter_id, setup_b.president, "member", setup_a.member)

    resp = await client.post(
        "/jobs", json=_job(setup_b.chapter_id), headers=setup_a.member.headers
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "alumni_or_eboard_only"

    # ...and their own chapter still works, so this is a scope fix and not a lockout.
    own = await client.post(
        "/jobs", json=_job(setup_a.chapter_id), headers=setup_a.member.headers
    )
    assert own.status_code == 201, own.text


# ---- GET /jobs: who may read ----


async def test_network_wide_job_does_not_reach_the_whole_network(
    client: AsyncClient, make_user: MakeUser, make_chapter_with: MakeChapterWith
) -> None:
    """The distribution half of c242.

    A chapter_id=None post reaches people who share a chapter with the poster, and
    nobody else. The two outsiders here — a member of an unrelated chapter and a
    brand-new account with no membership at all — are precisely who used to receive
    every network-wide apply_url on the platform.
    """
    poster_setup = await make_chapter_with("alumni")
    created = await client.post(
        "/jobs", json=_job(None), headers=poster_setup.member.headers
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    # The poster sees their own post.
    own = await client.get("/jobs", headers=poster_setup.member.headers)
    assert own.status_code == 200, own.text
    assert [job["id"] for job in own.json()] == [job_id]

    # So does a plain member of the poster's chapter — the intended audience.
    chapter_mate = await make_user("Chapter Mate")
    await _join(
        client,
        poster_setup.chapter_id,
        poster_setup.president,
        "member",
        chapter_mate,
    )
    mate_view = await client.get("/jobs", headers=chapter_mate.headers)
    assert mate_view.status_code == 200, mate_view.text
    assert [job["id"] for job in mate_view.json()] == [job_id]

    # A member of an unrelated chapter does not.
    outsider = (await make_chapter_with("member")).member
    outsider_view = await client.get("/jobs", headers=outsider.headers)
    assert outsider_view.status_code == 200, outsider_view.text
    assert outsider_view.json() == []

    # Neither does an account that belongs to nothing, whatever it calls itself.
    stranger = await make_user("Stranger", account_type="alumni")
    stranger_view = await client.get("/jobs", headers=stranger.headers)
    assert stranger_view.status_code == 200, stranger_view.text
    assert stranger_view.json() == []


async def test_chapter_scoped_job_stays_inside_its_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Unchanged behaviour, asserted so the read-scope rewrite cannot lose it."""
    setup_a = await make_chapter_with("member")
    created = await client.post(
        "/jobs", json=_job(setup_a.chapter_id), headers=setup_a.president.headers
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    inside = await client.get("/jobs", headers=setup_a.member.headers)
    assert inside.status_code == 200, inside.text
    assert [job["id"] for job in inside.json()] == [job_id]

    outsider = (await make_chapter_with("member")).member
    outside = await client.get("/jobs", headers=outsider.headers)
    assert outside.status_code == 200, outside.text
    assert outside.json() == []
