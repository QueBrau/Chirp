"""GET /jobs resolves the poster's display name server-side (board c59).

Why this test exists: the Jobs screen used to call mockUserById(job.posted_by),
resolving a REAL uuid against a hardcoded mock table. It never matched, so every
job on the alumni board rendered "Posted by Alumni" — wrong for every job ever
posted, and invisible in code review because the fallback looked deliberate.

There is no GET /users/{id}, so the name has to ride on the job row. These tests
assert it does, which is the anti-drift guard: drop the join and they fail.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def test_job_list_carries_the_posters_display_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The listed job names its poster, rather than leaving the client a bare uuid."""
    setup = await make_chapter_with("president")

    created = await client.post(
        "/jobs",
        json={
            "chapter_id": setup.chapter_id,
            "title": "Summer Analyst Intern",
            "company": "Northgate Capital",
            "location": "Chicago, IL",
            "description": "Paid summer internship on the private credit team.",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    listed = await client.get("/jobs", headers=setup.president.headers)
    assert listed.status_code == 200, listed.text

    jobs = listed.json()
    assert len(jobs) == 1
    job = jobs[0]

    # The uuid is still there for anything that needs identity...
    assert job["posted_by"] == setup.president.id
    # ...but the name is what the screen renders, and it must be the real one.
    assert job["posted_by_name"] == "Chapter President"


async def test_network_wide_job_also_carries_the_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A chapter_id=None job is visible network-wide and still names its poster.

    Worth asserting separately: the join is on posted_by, not on chapter, so a
    network-wide job must not lose the name just because it has no chapter.
    """
    setup = await make_chapter_with("president")

    created = await client.post(
        "/jobs",
        json={
            "chapter_id": None,
            "title": "Associate Product Manager",
            "company": "Harbor Health",
            "location": "Austin, TX",
            "description": "Rotational APM program for new grads.",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    listed = await client.get("/jobs", headers=setup.president.headers)
    assert listed.status_code == 200, listed.text

    jobs = listed.json()
    assert len(jobs) == 1
    assert jobs[0]["chapter_id"] is None
    assert jobs[0]["posted_by_name"] == "Chapter President"


async def test_name_is_the_poster_not_the_caller(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The join must resolve the POSTER, not whoever is reading the list.

    This is the failure the other two tests cannot catch: if the join were wired
    to the authenticated caller, both would still pass whenever the poster and
    the reader are the same person, which is the easy case to write.
    """
    setup = await make_chapter_with("member")

    created = await client.post(
        "/jobs",
        json={
            "chapter_id": setup.chapter_id,
            "title": "Backend Engineer",
            "company": "Harbor Health",
            "location": "Remote",
            "description": "New grad backend role.",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    # A DIFFERENT member of the same chapter reads the list.
    listed = await client.get("/jobs", headers=setup.member.headers)
    assert listed.status_code == 200, listed.text

    jobs = listed.json()
    assert len(jobs) == 1
    assert jobs[0]["posted_by"] == setup.president.id
    assert jobs[0]["posted_by_name"] == "Chapter President"
    assert jobs[0]["posted_by_name"] != "Chapter Member"
