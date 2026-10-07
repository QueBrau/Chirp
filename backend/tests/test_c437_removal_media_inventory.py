"""c437 synthetic fulfillment evidence for content removal and media surfaces.

These tests use real FastAPI routes and the disposable PostgreSQL database. The only
double is the GCS signing boundary; no provider or production object is touched.
The final test deliberately records the remaining known-copy gap: removing one post
does not remove a shared object while another live post still references it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.services import media_entitlement, storage_service
from tests.conftest import MakeChapterWith, set_campus, verify_campus

BUCKET = "c437-synthetic-media"
SECRET = "c437-synthetic-secret"
OBJECT = "posts/c437/shared.jpg"


@pytest.fixture(autouse=True)
def _synthetic_media_boundary(monkeypatch: pytest.MonkeyPatch):
    settings = storage_service.get_settings()
    monkeypatch.setattr(settings, "media_bucket_name", BUCKET)
    monkeypatch.setattr(settings, "media_signing_secret", SECRET)
    monkeypatch.setattr(settings, "app_public_base_url", "https://synthetic.invalid")

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def generate_signed_url(self, **kwargs):
            return f"https://storage.googleapis.com/{BUCKET}/{self.name}?synthetic=1"

    bucket = SimpleNamespace(blob=lambda name: FakeBlob(name))
    monkeypatch.setattr(storage_service, "_storage_client", lambda: SimpleNamespace(bucket=lambda _: bucket))
    import google.auth

    monkeypatch.setattr(
        google.auth,
        "default",
        lambda: (
            SimpleNamespace(
                service_account_email="synthetic@invalid",
                token="synthetic-token",
                refresh=lambda request: None,
            ),
            "synthetic-project",
        ),
    )
    storage_service._signed_read_cache.clear()
    media_entitlement._reset_memo_for_tests()
    yield
    storage_service._signed_read_cache.clear()
    media_entitlement._reset_memo_for_tests()


async def _insert_post_with_media(chapter_id: str, author_id: str, media_url: str) -> str:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO posts (id, chapter_id, campus_id, author_id, body, media_urls, "
                "audience, post_type, created_at) "
                "SELECT gen_random_uuid(), :chapter, c.campus_id, :author, 'synthetic photo', "
                "ARRAY[:media], 'org', 'photo', now() FROM chapters c WHERE c.id = :chapter "
                "RETURNING id"
            ),
            {"chapter": chapter_id, "author": author_id, "media": media_url},
        )
        post_id = str(result.scalar_one())
        await session.commit()
    return post_id


async def test_moderation_removal_disappears_from_chirp_feed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    await verify_campus(setup.president.id)
    detail = await client.get(f"/chapters/{setup.chapter_id}", headers=setup.president.headers)
    campus_id = detail.json()["campus_id"]

    created = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "synthetic removable chirp"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    chirp_id = created.json()["id"]
    before = await client.get(f"/campuses/{campus_id}/chirps", headers=setup.president.headers)
    assert any(item["id"] == chirp_id for item in before.json())

    removed = await client.post(
        f"/moderation/chirps/{chirp_id}/remove",
        json={"reason": "synthetic safety drill"},
        headers=setup.president.headers,
    )
    assert removed.status_code == 204, removed.text
    after = await client.get(f"/campuses/{campus_id}/chirps", headers=setup.president.headers)
    assert all(item["id"] != chirp_id for item in after.json())


async def test_moderation_removal_disappears_from_comment_thread(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    await verify_campus(setup.president.id)
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "synthetic post"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]
    comment = await client.post(
        f"/posts/{post_id}/comments",
        json={"body": "synthetic removable comment"},
        headers=setup.president.headers,
    )
    assert comment.status_code == 201, comment.text
    comment_id = comment.json()["id"]
    before = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    assert any(item["id"] == comment_id for item in before.json())

    removed = await client.post(
        "/moderation/content/remove",
        json={"target_type": "comment", "target_id": comment_id, "reason": "synthetic safety drill"},
        headers=setup.president.headers,
    )
    assert removed.status_code == 204, removed.text
    after = await client.get(f"/posts/{post_id}/comments", headers=setup.president.headers)
    assert all(item["id"] != comment_id for item in after.json())


async def test_shared_media_object_remains_until_every_live_reference_is_removed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Evidence of the remaining known-copy/provider gap.

    Both posts reference one object. Removing one post revokes neither the object nor
    the second post's entitlement, so the same token still redirects. Removing the
    second post finally makes the capability endpoint deny. No automatic GCS delete is
    attempted by moderation; permanent object cleanup needs a separate operator/job
    design that can scan known identical references safely.
    """
    setup = await make_chapter_with("president")
    media_url = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    first = await _insert_post_with_media(setup.chapter_id, setup.president.id, media_url)
    second = await _insert_post_with_media(setup.chapter_id, setup.president.id, media_url)
    token = storage_service.mint_media_token(OBJECT, setup.president.id)

    baseline = await client.get(f"/media/{token}")
    assert baseline.status_code == 302, baseline.text

    remove_first = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": first, "reason": "synthetic safety drill"},
        headers=setup.president.headers,
    )
    assert remove_first.status_code == 204, remove_first.text
    media_entitlement._reset_memo_for_tests()
    still_live = await client.get(f"/media/{token}")
    assert still_live.status_code == 302, still_live.text

    remove_second = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": second, "reason": "synthetic safety drill"},
        headers=setup.president.headers,
    )
    assert remove_second.status_code == 204, remove_second.text
    media_entitlement._reset_memo_for_tests()
    revoked = await client.get(f"/media/{token}")
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "media_access_revoked"
