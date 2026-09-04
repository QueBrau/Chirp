"""c308: founding a chapter must not mint campus moderation.

WHY THIS FILE EXISTS BEFORE THE FEATURE IT PROTECTS. braul asked for self-serve org
creation. POST /chapters already works and already makes its creator a president; the
only thing standing between any user and that is the is_platform_admin gate (c28). It
cannot simply be removed, because moderation.py scopes GET /moderation/reports to
"campuses where the caller is active e-board" and reports carry forwarded_plaintext of
reported E2EE messages. Founding a chapter makes you e-board on that chapter's campus,
so ungating creation on its own would let anyone mint a throwaway chapter on any campus
and read that campus's moderation queue — SECURITY-REVIEW finding 1, reached through a
new door.

So the decoupling lands and is verified FIRST, and the property these tests pin is the
one that makes the later ungating safe rather than merely narrower.

TWO THINGS EVERY TEST HERE IS BUILT TO AVOID, both of which would pass while proving
nothing:

  1. A 403 that comes from somewhere else. Campus moderation already requires a
     verified .edu (c108) and already requires matching the target's campus (finding
     1's fix), so an unapproved chapter's officer can be refused for three different
     reasons at once. Every refusal below is paired with the SAME call succeeding once
     the only thing that changed is chapters.moderation_approved — same user, same
     endpoint, same target, flag flipped. If the pair does not invert, the discriminator
     was not approval.
  2. An empty queue that was always empty. "They see no reports" is worthless unless a
     report exists that they would otherwise see, so each queue assertion first proves
     an approved officer on that same campus CAN see the very same report id.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import (
    ApiUser,
    ChapterSetup,
    MakeCampus,
    MakeChapterWith,
    MakeUser,
    _grant_platform_admin,
    approve_chapter_moderation,
    set_campus,
    verify_campus,
)


async def _make_chapter_on_campus(
    client: AsyncClient,
    make_user: MakeUser,
    campus_id: str,
    president_name: str,
    *,
    approved: bool,
) -> ChapterSetup:
    """A chapter on a CALLER-SUPPLIED campus, approved or not.

    conftest's make_chapter_with mints a fresh campus per call and approves by default;
    this card's whole subject is two chapters sharing one campus with different approval
    states, which that factory cannot express.
    """
    president = await make_user(president_name)
    await _grant_platform_admin(president.id)
    created = await client.post(
        "/chapters",
        json={
            "campus_id": campus_id,
            "org_name": f"Org {uuid.uuid4().hex[:6]}",
            "chapter_name": president_name,
        },
        headers=president.headers,
    )
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]
    if approved:
        await approve_chapter_moderation(chapter_id)
    return ChapterSetup(chapter_id=chapter_id, member=president, president=president)


async def _report_on(client: AsyncClient, reporter: ApiUser, post_id: str) -> str:
    report = await client.post(
        "/moderation/reports",
        json={"target_type": "post", "target_id": post_id, "reason": "spam"},
        headers=reporter.headers,
    )
    assert report.status_code == 201, report.text
    return report.json()["id"]


async def test_a_freshly_founded_chapter_sees_none_of_its_campus_report_queue(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The property that makes ungating creation safe.

    Both presidents are active e-board on the SAME campus, so under the pre-c308 check
    ("e-board of any active chapter", then scope to the target's campus) they were
    indistinguishable — which is precisely the vulnerability: with creation ungated, the
    second one is anybody who typed an org name.
    """
    campus_id = await make_campus()
    established = await _make_chapter_on_campus(
        client, make_user, campus_id, "Established President", approved=True
    )
    fresh = await _make_chapter_on_campus(
        client, make_user, campus_id, "Fresh President", approved=False
    )

    post = await client.post(
        f"/chapters/{established.chapter_id}/posts",
        json={"body": "reported content"},
        headers=established.president.headers,
    )
    assert post.status_code == 201, post.text
    reporter = await make_user("Reporter")
    report_id = await _report_on(client, reporter, post.json()["id"])

    # THE CONDITION IS REAL FIRST: this campus has a report, and an approved officer of
    # this campus can see it. Without this line the assertion below would pass just as
    # happily against an empty queue.
    established_view = await client.get(
        "/moderation/reports", headers=established.president.headers
    )
    assert established_view.status_code == 200, established_view.text
    assert any(r["id"] == report_id for r in established_view.json()), (
        "setup is broken: an approved officer must see their own campus's report"
    )

    # The fresh chapter's president: same campus, same active e-board role, refused at
    # the door. Not an empty list — they never reach the query.
    fresh_view = await client.get("/moderation/reports", headers=fresh.president.headers)
    assert fresh_view.status_code == 403, fresh_view.text
    assert fresh_view.json() == {"detail": "insufficient_role"}

    # AND THE FLAG IS WHAT DID IT: nothing else about this user changes.
    await approve_chapter_moderation(fresh.chapter_id)
    after_approval = await client.get(
        "/moderation/reports", headers=fresh.president.headers
    )
    assert after_approval.status_code == 200, after_approval.text
    assert any(r["id"] == report_id for r in after_approval.json()), (
        "approval must restore exactly the access the backfill gives established chapters"
    )


async def test_every_sitting_eboard_role_keeps_the_queue(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """braul's ruling Sep 4: "keep moderation access on eboard only" — so the backfill is
    like-for-like across ROLES, not presidents-only (an earlier narrowing, withdrawn).

    A treasurer is the sharpest case: they can read the queue today, they are not the
    founder, and a per-user grant scheme would have quietly dropped their successor.
    """
    setup = await make_chapter_with("treasurer")
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "reported content"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    reporter = await make_user("Reporter")
    report_id = await _report_on(client, reporter, post.json()["id"])

    view = await client.get("/moderation/reports", headers=setup.member.headers)
    assert view.status_code == 200, view.text
    assert any(r["id"] == report_id for r in view.json()), (
        "a sitting treasurer of an approved chapter must keep exactly today's access"
    )


async def test_approval_not_verification_is_what_gates_a_campus_chirp_removal(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """Removing a campus chirp is the most powerful campus act, and it has THREE gates:
    e-board somewhere, e-board on this campus, and a verified .edu (c108).

    This test deliberately satisfies the other two so the only remaining variable is
    approval. Without pinning and verifying the fresh president first, the 403 below
    would arrive from the c108 verification check and the test would pass with the c308
    change reverted — green, and about nothing.
    """
    campus_id = await make_campus()
    fresh = await _make_chapter_on_campus(
        client, make_user, campus_id, "Fresh President", approved=False
    )
    await set_campus(fresh.president.id, campus_id)
    await verify_campus(fresh.president.id)

    chirper = await make_user("Chirper")
    await set_campus(chirper.id, campus_id)
    await verify_campus(chirper.id)
    chirp = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "anonymous chirp"},
        headers=chirper.headers,
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    refused = await client.post(
        f"/moderation/chirps/{chirp_id}/remove",
        json={"reason": "test"},
        headers=fresh.president.headers,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "insufficient_role"}, (
        "a verified, campus-matched officer of an UNAPPROVED chapter must still be "
        "refused — and refused as insufficient_role, never as campus_unverified, which "
        "would tell them a .edu was the only thing in their way"
    )

    # Flip the one flag; the identical call must now succeed.
    await approve_chapter_moderation(fresh.chapter_id)
    allowed = await client.post(
        f"/moderation/chirps/{chirp_id}/remove",
        json={"reason": "test"},
        headers=fresh.president.headers,
    )
    assert allowed.status_code == 204, allowed.text


async def test_an_unapproved_chapters_president_still_governs_their_own_chapter(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The decoupling must not overreach into chapter self-governance.

    This is the fact the whole design rests on: a president's power over their own
    chapter's posts does not live in moderation.py at all — feed.py's
    DELETE /chapters/{chapter_id}/posts/{post_id} is author-or-president gated on
    membership alone. If that stopped being true, gating the moderation router on
    approval would silently take an unapproved org's ability to run itself, and c308
    would have traded one bug for another.
    """
    campus_id = await make_campus()
    fresh = await _make_chapter_on_campus(
        client, make_user, campus_id, "Fresh President", approved=False
    )
    post = await client.post(
        f"/chapters/{fresh.chapter_id}/posts",
        json={"body": "our own chapter's post"},
        headers=fresh.president.headers,
    )
    assert post.status_code == 201, post.text

    deleted = await client.delete(
        f"/chapters/{fresh.chapter_id}/posts/{post.json()['id']}",
        headers=fresh.president.headers,
    )
    assert deleted.status_code == 204, deleted.text


async def test_an_unapproved_chapters_officer_is_refused_by_the_moderation_router(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A KNOWN, DELIBERATE CONSEQUENCE, pinned here so it is a decision and not a
    surprise found later in production.

    Approval gates the whole moderation router rather than only its campus tier. The
    alternative — gating only campus-tier acts — was rejected because it would still let
    a freshly founded chapter DISMISS reports about other chapters' org content on the
    same campus, which is a privilege minted by founding, exactly what the card forbids.

    The cost, stated plainly and more sharply than the first draft of this docstring
    put it: in an unapproved chapter a NON-president officer loses own-chapter content
    removal ENTIRELY. This route refuses them, and feed.py's delete does not take them
    either — it is author-or-PRESIDENT, so a secretary or treasurer who did not write the
    post has no remaining path to remove it. The president is genuinely unaffected; every
    other officer is fully blocked, not merely redirected. For every chapter that existed
    at migration 0031 this is unreachable — the backfill approved all of them — so nobody
    currently holding it loses it, and it becomes real the day creation is ungated.
    """
    setup = await make_chapter_with("treasurer", approve_moderation=False)
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "our own chapter's post"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text

    refused = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": "x"},
        headers=setup.member.headers,
    )
    assert refused.status_code == 403, refused.text

    await approve_chapter_moderation(setup.chapter_id)
    allowed = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": "x"},
        headers=setup.member.headers,
    )
    assert allowed.status_code == 204, allowed.text


async def test_a_real_officer_cannot_reach_a_second_campus_by_founding_there(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """THE ACTUAL ATTACK the ungating would enable, and the only test here that pins the
    per-campus half of the fix on its own.

    Every other refusal in this file is against someone whose ONLY chapter is
    unapproved, so _require_any_moderator turns them away at the door and the campus
    scoping is never consulted. That is the easy case. The real one is an attacker who
    is already a legitimate officer somewhere: they walk through the entry dependency on
    their genuine chapter, and the only thing between them and a second campus's queue —
    carrying forwarded_plaintext of reported E2EE messages — is whether the campus
    scoping ALSO requires approval. Fix the entry dependency alone and this test is the
    one that still fails.

    Campus A is theirs by right. Campus B they simply founded on.
    """
    campus_a = await make_campus()
    campus_b = await make_campus()

    legit = await _make_chapter_on_campus(
        client, make_user, campus_a, "Legitimate President", approved=True
    )
    attacker = legit.president

    # The same person founds on campus B. They keep is_platform_admin from the helper,
    # which is exactly what self-serve creation would hand every user for free.
    founded = await client.post(
        "/chapters",
        json={
            "campus_id": campus_b,
            "org_name": f"Throwaway {uuid.uuid4().hex[:6]}",
            "chapter_name": "Minted",
        },
        headers=attacker.headers,
    )
    assert founded.status_code == 201, founded.text

    # A report exists on campus B, and campus B's own approved officer can see it —
    # otherwise the assertion below would pass against an empty queue.
    victim = await _make_chapter_on_campus(
        client, make_user, campus_b, "Campus B President", approved=True
    )
    post = await client.post(
        f"/chapters/{victim.chapter_id}/posts",
        json={"body": "campus B content"},
        headers=victim.president.headers,
    )
    assert post.status_code == 201, post.text
    reporter = await make_user("Reporter")
    report_id = await _report_on(client, reporter, post.json()["id"])

    victim_view = await client.get(
        "/moderation/reports", headers=victim.president.headers
    )
    assert victim_view.status_code == 200, victim_view.text
    assert any(r["id"] == report_id for r in victim_view.json()), (
        "setup is broken: campus B's approved officer must see campus B's report"
    )

    # The attacker passes the entry dependency on campus A's real chapter — 200, not
    # 403 — and must still see nothing from campus B.
    attacker_view = await client.get("/moderation/reports", headers=attacker.headers)
    assert attacker_view.status_code == 200, attacker_view.text
    assert all(r["id"] != report_id for r in attacker_view.json()), (
        "founding a chapter on campus B must not put campus B's report queue in reach"
    )

    # Nor act on it.
    refused = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "no action"},
        headers=attacker.headers,
    )
    # 403 specifically, not merely "not 2xx": this endpoint has its own 422, and an
    # assertion that accepted any error would have passed on a malformed body while
    # proving nothing about authorization. It did exactly that on the first run here.
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "insufficient_role"}
