"""Alumni job board: GET /jobs joins the poster's real display_name.

Regression coverage for the mock-identity bug where the mobile client
resolved `posted_by` (a real user uuid) against a mock user array and always
missed, showing "Posted by Alumni" for every job. The fix moves the join
server-side onto `posted_by_name`.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


def _job_body(title: str = "Summer Analyst") -> dict[str, str]:
    return {
        "title": title,
        "company": "Acme Corp",
        "location": "Remote",
        "description": "Great opportunity for a rising senior.",
    }


async def test_list_jobs_includes_real_poster_display_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GET /jobs returns posted_by_name matching the poster's actual display_name,
    not null and not a mock fallback."""
    setup = await make_chapter_with("president")

    created = await client.post(
        "/jobs",
        json=_job_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    listed = await client.get("/jobs", headers=setup.president.headers)
    assert listed.status_code == 200, listed.text
    jobs = listed.json()
    assert len(jobs) == 1
    assert jobs[0]["posted_by"] == setup.president.id
    assert jobs[0]["posted_by_name"] == "Chapter President"


async def test_create_job_response_has_null_posted_by_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """POST /jobs doesn't run the join — posted_by_name is null there, which the
    client is expected to handle (falls back to 'Alumni')."""
    setup = await make_chapter_with("president")

    created = await client.post(
        "/jobs",
        json=_job_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["posted_by_name"] is None
