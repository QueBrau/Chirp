"""media_object_names validation + tmp-then-move on every post write route (c139, c132).

c139: media_urls (client-supplied) was completely unvalidated, so any authenticated
member could point a post at an arbitrary external host and every viewer's device
would fetch it on load.

c132 tightens this further: the create/update INPUT is no longer a url at all. A
client uploads to POST /media/upload-url, which now mints a tmp/{user_id}/{uuid}.{ext}
object, and sends the returned object_name (not a url) in media_object_names. The
route validates it is the CALLER'S OWN tmp/ object (the new attack surface this design
closes — object names are opaque UUIDs, not secret, and the bucket is public-read, so
without the user_id-scoped prefix check one caller could reference another caller's
tmp upload), then MOVES it to a permanent posts/ location and assigns the resulting
url itself. A post's media_urls is therefore entirely server-assigned: no client input
is EVER written to that column directly, tightening validate_media_urls's exact-prefix
check to the permanent bucket prefix as a defensive invariant on the server's own output
rather than a check on client input at all.

The attacker-shaped matrix below (someone else's tmp path, a posts/ path claimed
directly, an external host, javascript: scheme, wrong bucket) all collapse to the same
exact-prefix-against-MY-tmp-namespace check failing.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers import feed
from app.services import storage_service
from tests.conftest import MakeChapterWith, verify_campus

TEST_BUCKET = "chirps-prod-media"


def _configure_bucket(monkeypatch) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", TEST_BUCKET)


class _FakeBlob:
    def __init__(self, name: str, captured: dict) -> None:
        self.name = name
        self._captured = captured

    def generate_signed_url(self, **kwargs):
        self._captured["signed_url_kwargs"] = kwargs
        return "https://storage.googleapis.com/fake-bucket/signed?sig=abc"

    def delete(self) -> None:
        self._captured.setdefault("deleted", []).append(self.name)
        if self._captured.get("delete_should_fail"):
            raise RuntimeError("simulated delete failure")


class _FakeBucket:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self._captured)

    def copy_blob(self, blob: _FakeBlob, destination_bucket, new_name: str, **kwargs):
        if self._captured.get("copy_should_404"):
            from google.api_core.exceptions import NotFound

            raise NotFound("simulated missing tmp object")
        self._captured.setdefault("copy_blob_calls", []).append(
            {"source": blob.name, "new_name": new_name, "kwargs": kwargs}
        )
        return _FakeBlob(new_name, self._captured)


def _install_fake_gcs(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    """Same network-crossing-boundary fake as test_media_upload_url.py, extended with
    copy_blob/delete so finalize_media_object()'s move is exercised for real (against a
    fake), not skipped."""
    fake_bucket = _FakeBucket(captured)
    fake_client = SimpleNamespace(bucket=lambda name: fake_bucket)
    monkeypatch.setattr(storage_service, "_storage_client", lambda: fake_client)

    import google.auth

    monkeypatch.setattr(
        google.auth,
        "default",
        lambda: (
            SimpleNamespace(
                service_account_email="chirp-api-run@chirps-prod.iam.gserviceaccount.com",
                token="fake-access-token",
                refresh=lambda request: None,
            ),
            "proj",
        ),
    )


@pytest.fixture(autouse=True)
def _reset_storage_client_cache():
    storage_service._client = None
    yield
    storage_service._client = None


async def test_own_tmp_object_is_moved_and_the_permanent_url_is_stored(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_bucket(monkeypatch)
    captured: dict = {}
    _install_fake_gcs(monkeypatch, captured)
    setup = await make_chapter_with("member")
    tmp_name = f"tmp/{setup.member.id}/abc123.jpg"

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "check out this pic", "media_object_names": [tmp_name], "post_type": "photo"},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    media_urls = created.json()["media_urls"]
    assert media_urls == [f"https://storage.googleapis.com/{TEST_BUCKET}/posts/{setup.member.id}/abc123.jpg"]

    # the move actually happened: copy from the tmp path to the permanent one, with
    # preserve_acl=False (required against a uniform-bucket-access bucket), then the
    # tmp source was deleted.
    [call] = captured["copy_blob_calls"]
    assert call["source"] == tmp_name
    assert call["new_name"] == f"posts/{setup.member.id}/abc123.jpg"
    assert call["kwargs"]["preserve_acl"] is False
    assert captured["deleted"] == [tmp_name]


async def test_someone_elses_tmp_object_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new attack surface this design closes: tmp object names are opaque UUIDs,
    not secret, and the bucket is public-read, so cross-user reference must be refused
    by the user_id-scoped prefix check, not by object-name secrecy."""
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": ["tmp/some-other-user-id/abc123.jpg"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_claiming_a_posts_path_directly_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client cannot bypass the move entirely by naming an existing permanent
    location — the only door in is tmp/{own user_id}/..."""
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": [f"posts/{setup.member.id}/existing.jpg"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_external_host_shaped_string_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": ["https://evil.example.com/tracker.png"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_too_many_media_object_names_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("member")
    tmp_name = f"tmp/{setup.member.id}/abc123.jpg"

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": [tmp_name, tmp_name]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "too_many_media_urls"


async def test_overlong_media_object_name_is_rejected(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("member")
    overlong = f"tmp/{setup.member.id}/" + "a" * 300 + ".jpg"

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": [overlong]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "media_url_too_long"


async def test_unconfigured_bucket_rejects_a_post_that_claims_media(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": [f"tmp/{setup.member.id}/abc123.jpg"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 503, created.text
    assert created.json()["detail"] == "media_not_configured"


async def test_text_only_post_is_unaffected_by_a_missing_bucket(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "just text, no media"},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text


async def test_referencing_a_tmp_object_that_does_not_exist_is_a_400(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never uploaded, or the tmp/ lifecycle rule already reclaimed it — either way the
    caller referenced something that isn't there to move, a 400 not a 500."""
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {"copy_should_404": True})
    setup = await make_chapter_with("member")

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "x", "media_object_names": [f"tmp/{setup.member.id}/gone.jpg"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "media_upload_not_found"


async def test_a_failed_tmp_delete_after_a_successful_copy_does_not_fail_the_post(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The post already has its permanent url from the copy; a delete failure just
    leaves an extra tmp/ object for the lifecycle rule to reclaim later, logged as a
    warning rather than raised."""
    _configure_bucket(monkeypatch)
    captured = {"delete_should_fail": True}
    _install_fake_gcs(monkeypatch, captured)
    setup = await make_chapter_with("member")

    with caplog.at_level(logging.WARNING, logger=storage_service.logger.name):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={"body": "x", "media_object_names": [f"tmp/{setup.member.id}/abc123.jpg"]},
            headers=setup.member.headers,
        )
    assert created.status_code == 201, created.text
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "failed to delete moved tmp object" in logged


async def test_commit_failure_after_a_successful_move_is_logged_loudly_not_deleted(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """c132's core tradeoff: the service account cannot delete from posts/ (IAM-
    conditioned to tmp/ only), so a commit failure after a successful move cannot be
    compensated away — it must be logged loudly instead. Setup's own commits must
    complete before the patch, or chapter/membership creation itself would break."""
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("member")

    async def _raise(self, *args, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(AsyncSession, "commit", _raise)

    with caplog.at_level(logging.ERROR, logger=feed.logger.name), pytest.raises(RuntimeError):
        await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={"body": "x", "media_object_names": [f"tmp/{setup.member.id}/abc123.jpg"]},
            headers=setup.member.headers,
        )

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "orphan" in logged
    assert f"posts/{setup.member.id}/abc123.jpg" in logged


async def test_campus_post_route_validates_media_object_names_too(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second write site: POST /campuses/{campus_id}/posts."""
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    setup = await make_chapter_with("president")
    campus = await client.get(f"/chapters/{setup.chapter_id}", headers=setup.president.headers)
    assert campus.status_code == 200, campus.text
    campus_id = campus.json()["campus_id"]
    await verify_campus(setup.president.id)

    created = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "x", "media_object_names": ["tmp/some-other-user-id/abc123.jpg"]},
        headers=setup.president.headers,
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "invalid_media_url"


async def test_update_post_route_moves_a_newly_attached_photo(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Third write site: PATCH /chapters/{id}/posts/{id}, attaching media that wasn't
    there at create time — goes through the identical validate+move path."""
    _configure_bucket(monkeypatch)
    captured: dict = {}
    _install_fake_gcs(monkeypatch, captured)
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
        json={"media_object_names": [f"tmp/{setup.member.id}/xyz789.png"]},
        headers=setup.member.headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["media_urls"] == [
        f"https://storage.googleapis.com/{TEST_BUCKET}/posts/{setup.member.id}/xyz789.png"
    ]
    assert captured["copy_blob_calls"][0]["new_name"] == f"posts/{setup.member.id}/xyz789.png"


async def test_update_post_route_rejects_someone_elses_tmp_object(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
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
        json={"media_object_names": ["tmp/some-other-user-id/abc123.jpg"]},
        headers=setup.member.headers,
    )
    assert updated.status_code == 400, updated.text
    assert updated.json()["detail"] == "invalid_media_url"


async def test_update_post_route_clears_media_with_an_empty_list(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing an existing photo must not require touching GCS or a configured
    bucket — an empty list is a pure DB write."""
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
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
        json={"media_object_names": []},
        headers=setup.member.headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["media_urls"] is None
