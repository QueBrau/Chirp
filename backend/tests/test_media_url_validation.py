"""media_urls validation on every post write route (board card c139, security MEDIUM).

Before this, media_urls was completely unvalidated: any authenticated member could
point a post at an arbitrary external host, and every viewer's device would fetch it
on load — a working read-receipt oracle in a small chapter, using the API exactly as
designed (security's finding, cited against MediaPostCard rendering `uri` directly).

The fix is storage_service.validate_media_urls(): an exact-prefix allowlist against
settings.media_bucket_name (never a hardcoded bucket name, so a test/local deployment
validates against its own configured value), a hard cap of one url for alpha (matches
the compose UI's single-photo scope), and a length bound so an unbounded string can't
reach the database. Wired into all three write sites security cited: POST
/chapters/{id}/posts, POST /campuses/{id}/posts, and PATCH /chapters/{id}/posts/{id}.

The attacker-shaped matrix below (external host, wrong scheme, right host wrong
bucket, javascript: scheme) all collapse to the same exact-prefix check failing —
that's the point of an allowlist over a denylist: one check, not four.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.services import storage_service
from tests.conftest import MakeChapterWith, verify_campus

TEST_BUCKET = "chirps-prod-media"
VALID_URL = f"https://storage.googleapis.com/{TEST_BUCKET}/posts/some-user/abc123.png"


def _configure_bucket(monkeypatch) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", TEST_BUCKET)


async def test_valid_media_url_is_accepted(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "check out this pic", "media_urls": [VALID_URL], "post_type": "photo"},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["media_urls"] == [VALID_URL]


async def test_external_https_url_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_urls": ["https://evil.example.com/tracker.png"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_http_scheme_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """Same host and bucket, wrong scheme — the exact-prefix check is scheme-sensitive."""
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={
            "body": "x",
            "media_urls": [f"http://storage.googleapis.com/{TEST_BUCKET}/posts/x/y.png"],
        },
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_right_host_wrong_bucket_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """A real GCS url, just not OUR bucket — proves the check isn't just a domain check."""
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={
            "body": "x",
            "media_urls": ["https://storage.googleapis.com/some-other-bucket/x/y.png"],
        },
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_javascript_scheme_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_urls": ["javascript:alert(document.cookie)"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_too_many_media_urls_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """Alpha cap is one photo, matching the compose UI's single-photo scope."""
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_urls": [VALID_URL, VALID_URL]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "too_many_media_urls"


async def test_overlong_media_url_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")
    padding = "a" * 600
    overlong = f"https://storage.googleapis.com/{TEST_BUCKET}/posts/{padding}.png"

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_urls": [overlong]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "media_url_too_long"


async def test_unconfigured_bucket_rejects_a_post_that_claims_media(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """No bucket configured -> can't judge a media url as legitimate, so refuse with a
    clear reason (503, same shape as the upload-url route) rather than accept blind."""
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_urls": [VALID_URL]},
        headers=setup.member.headers,
    )
    assert created.status_code == 503, created.text
    assert created.json()["detail"] == "media_not_configured"


async def test_text_only_post_is_unaffected_by_a_missing_bucket(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """The regression this design decision guards against: a text-only post must
    never require a configured media bucket to save."""
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "just text, no media"},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text


async def test_campus_post_route_validates_media_urls_too(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """Second write site security cited: POST /campuses/{campus_id}/posts."""
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("president")
    campus = await client.get(f"/chapters/{setup.chapter_id}", headers=setup.president.headers)
    assert campus.status_code == 200, campus.text
    campus_id = campus.json()["campus_id"]
    await verify_campus(setup.president.id)

    created = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "x", "media_urls": ["https://evil.example.com/tracker.png"]},
        headers=setup.president.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_update_post_route_validates_media_urls_too(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch
) -> None:
    """Third write site security cited: PATCH /chapters/{id}/posts/{id}."""
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "mine to edit"},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    updated = await client.patch(
        f"/chapters/{setup.chapter_id}/posts/{post_id}",
        json={"media_urls": ["https://evil.example.com/tracker.png"]},
        headers=setup.member.headers,
    )
    assert updated.status_code == 400, updated.text
    assert updated.json()["detail"] == "invalid_media_url"
