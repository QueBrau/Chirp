"""c184: linkedin_url (alumni profile) and apply_url (job posts) were persisted
as plain `str | None` with zero validation, and the mobile client opens them
blind via Linking.openURL — a verified phishing / intent-URI vector (c182
audit finding). Both fields must now reject anything that is not a well-formed
http(s) URL at the schema layer (422), while still accepting None and staying
silent about every other field on the same body.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import MakeUser

BAD_URLS = [
    "javascript:alert(1)",
    "intent://scan/#Intent;end",
    "data:text/html,<script>alert(1)</script>",
    "ftp://x",
    "https://" + "a" * 5000 + ".com",  # 5000+ chars, well past the 2048 cap
]


# ---- alumni profile: PUT /alumni/profile (linkedin_url) ----


@pytest.mark.parametrize("bad_url", BAD_URLS)
async def test_bad_linkedin_url_is_422(
    client: AsyncClient, make_user: MakeUser, bad_url: str
) -> None:
    user = await make_user()
    resp = await client.put(
        "/alumni/profile",
        json={"linkedin_url": bad_url},
        headers=user.headers,
    )
    assert resp.status_code == 422, resp.text


async def test_good_linkedin_url_is_accepted_and_persisted(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    resp = await client.put(
        "/alumni/profile",
        json={"linkedin_url": "https://linkedin.com/in/x"},
        headers=user.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["linkedin_url"] == "https://linkedin.com/in/x"


async def test_none_linkedin_url_is_accepted(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    resp = await client.put(
        "/alumni/profile",
        json={"linkedin_url": None},
        headers=user.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["linkedin_url"] is None


async def test_untouched_profile_fields_still_round_trip(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The validator must not affect any other field on the same body."""
    user = await make_user()
    resp = await client.put(
        "/alumni/profile",
        json={
            "grad_year": 2019,
            "company": "Harbor Health",
            "title": "Engineer",
            "industry": "Health Tech",
            "location": "Austin, TX",
            "linkedin_url": "https://linkedin.com/in/x",
            "open_to_mentoring": True,
        },
        headers=user.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grad_year"] == 2019
    assert body["company"] == "Harbor Health"
    assert body["title"] == "Engineer"
    assert body["industry"] == "Health Tech"
    assert body["location"] == "Austin, TX"
    assert body["open_to_mentoring"] is True


# ---- job posts: POST /jobs (apply_url) ----
#
# account_type="alumni" makes the caller is_eligible in create_job_post without
# any chapter setup (routers/alumni.py: `user.account_type == "alumni"`), and
# chapter_id=None posts network-wide, so these stay independent of chapter/
# membership plumbing entirely — the field under test is apply_url alone.


def _job_body(apply_url: str | None) -> dict[str, object]:
    return {
        "chapter_id": None,
        "title": "Summer Analyst",
        "company": "Northgate Capital",
        "location": "Chicago, IL",
        "description": "Paid summer internship.",
        "apply_url": apply_url,
    }


@pytest.mark.parametrize("bad_url", BAD_URLS)
async def test_bad_apply_url_is_422(
    client: AsyncClient, make_user: MakeUser, bad_url: str
) -> None:
    user = await make_user(account_type="alumni")
    resp = await client.post("/jobs", json=_job_body(bad_url), headers=user.headers)
    assert resp.status_code == 422, resp.text


async def test_good_apply_url_is_accepted_and_persisted(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user(account_type="alumni")
    resp = await client.post(
        "/jobs", json=_job_body("https://linkedin.com/in/x"), headers=user.headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["apply_url"] == "https://linkedin.com/in/x"


async def test_none_apply_url_is_accepted(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user(account_type="alumni")
    resp = await client.post("/jobs", json=_job_body(None), headers=user.headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["apply_url"] is None


async def test_untouched_job_fields_still_behave(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The validator must not affect required-field enforcement on the same body
    (title/company/location/description all keep their existing min_length=1)."""
    user = await make_user(account_type="alumni")
    resp = await client.post(
        "/jobs",
        json={
            "chapter_id": None,
            "title": "",
            "company": "Northgate Capital",
            "location": "Chicago, IL",
            "description": "Paid summer internship.",
            "apply_url": "https://linkedin.com/in/x",
        },
        headers=user.headers,
    )
    assert resp.status_code == 422, resp.text

    good = await client.post(
        "/jobs", json=_job_body("https://linkedin.com/in/x"), headers=user.headers
    )
    assert good.status_code == 201, good.text
    assert good.json()["title"] == "Summer Analyst"
    assert good.json()["company"] == "Northgate Capital"
