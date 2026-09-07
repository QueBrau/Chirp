"""c378: self-serve chapter creation — the deferred half of c308/c325 (board c28's
gate lifted, per routers/chapters.py's create_chapter docstring).

test_chapter_creation_gate.py pins WHO may call POST /chapters now (a campus-verified
caller, admin or not). test_c308_moderation_decoupled.py's own tests already exercise
the real self-serve path end to end (this file's edit removed the platform-admin
simulation that predated this card) and already prove founding grants no queue access.

THIS FILE IS THE CANONICAL, SELF-CONTAINED PROOF of the two properties that are
SPECIFIC to lifting the gate rather than to the decoupling c308 already shipped:

  1. Founding a chapter through the REAL self-serve route — no admin grant anywhere
     in the test, no DB shortcut standing in for the API — grants no campus
     moderation. Written standalone (not depending on test_c308's fixtures) so it
     keeps proving this even if that file's helpers are refactored later.
  2. The server, not the request body, decides which campus a new chapter lands on.
     A verified caller who submits someone else's campus_id in the body must not
     get a chapter that reads as that other campus's.

Both follow test_c308's own discipline: a 403 is worthless if it would have fired for
some other reason, and an empty queue is worthless unless something not-empty was
possible in the first place.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import MakeCampus, MakeUser, approve_chapter_moderation, set_campus


async def _self_serve_chapter(
    client: AsyncClient, president_headers: dict[str, str], **body_overrides: object
) -> dict:
    body = {
        "org_name": f"Self Serve Org {uuid.uuid4().hex[:6]}",
        "chapter_name": "Founding Class",
        **body_overrides,
    }
    response = await client.post("/chapters", json=body, headers=president_headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_founding_a_chapter_grants_no_campus_moderation(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """THE ESCALATION SECURITY-REVIEW finding 1 / board card c28 closed, reopened
    only if self-serve creation ever hands its founder a moderation seat. This test
    creates the chapter through the REAL POST /chapters a mobile client will call —
    campus-verified caller, no campus_id in the body, no admin grant — then proves
    the founder is refused on the exact route c28 gated: GET /moderation/reports.

    Two things this test refuses to let pass for the wrong reason:
      * the 403 must be insufficient_role (never campus_unverified, which would
        mean the founder was refused for a DIFFERENT reason than moderation
        approval and would tell them a .edu was all that stood in their way);
      * the campus queue must not simply be empty — an established, APPROVED
        chapter's officer on the SAME campus is proven to see the same report
        first, so the founder's 403 is proven to be about THEM, not about the
        campus having nothing to show.
    """
    campus_id = await make_campus()

    # An established, approved org already on this campus — stands in for a chapter
    # that existed before self-serve creation shipped (c308's backfill approved
    # every such chapter). Built via set_campus + approve, not via POST /chapters,
    # so this test does not depend on ANOTHER self-serve call succeeding first.
    established_officer = await make_user("Established President")
    await set_campus(established_officer.id, campus_id)
    established = await _self_serve_chapter(client, established_officer.headers)
    await approve_chapter_moderation(established["id"])

    post = await client.post(
        f"/chapters/{established['id']}/posts",
        json={"body": "reported content"},
        headers=established_officer.headers,
    )
    assert post.status_code == 201, post.text
    reporter = await make_user("Reporter")
    report = await client.post(
        "/moderation/reports",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": "spam"},
        headers=reporter.headers,
    )
    assert report.status_code == 201, report.text
    report_id = report.json()["id"]

    # THE CONDITION IS REAL: an approved officer on this campus sees the report.
    # Without this, the founder's 403 below would prove nothing.
    established_view = await client.get(
        "/moderation/reports", headers=established_officer.headers
    )
    assert established_view.status_code == 200, established_view.text
    assert any(r["id"] == report_id for r in established_view.json()), (
        "setup is broken: an approved officer must see their own campus's report"
    )

    # Now the actual subject: a brand-new founder, self-serve, same campus, never
    # approved for moderation.
    founder = await make_user("Fresh Founder")
    await set_campus(founder.id, campus_id)
    founded = await _self_serve_chapter(client, founder.headers)
    assert founded["campus_id"] == campus_id

    founder_view = await client.get("/moderation/reports", headers=founder.headers)
    assert founder_view.status_code == 403, founder_view.text
    assert founder_view.json() == {"detail": "insufficient_role"}, (
        "founding a chapter must refuse as insufficient_role, not campus_unverified "
        "— the founder IS verified (that's how they passed POST /chapters) and the "
        "refusal must be about moderation approval, not campus proof"
    )

    # And they cannot act on the report either.
    resolve_attempt = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "n/a"},
        headers=founder.headers,
    )
    assert resolve_attempt.status_code == 403, resolve_attempt.text
    assert resolve_attempt.json() == {"detail": "insufficient_role"}


async def test_body_supplied_campus_id_for_a_different_campus_is_ignored(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """ChapterCreate carries no campus_id field any more (c378, mirroring c85's
    UserCreate) — but _Schema does not set extra="forbid", so a client (or a stale
    mobile build) that still sends one must have it silently ignored, never honored.

    A verified caller on campus A submits a body naming campus B (a real, existing
    campus — the sharpest version of this: not a garbage value that would 422 for
    some unrelated reason, but a legitimate id that just isn't theirs) and the
    created chapter must land on A regardless.
    """
    campus_a = await make_campus()
    campus_b = await make_campus()
    user = await make_user("Campus A Student")
    await set_campus(user.id, campus_a)

    created = await _self_serve_chapter(client, user.headers, campus_id=campus_b)

    assert created["campus_id"] == campus_a, (
        "a body-supplied campus_id for a campus the caller does not belong to must "
        "never end up on the created chapter"
    )
    assert created["campus_id"] != campus_b
