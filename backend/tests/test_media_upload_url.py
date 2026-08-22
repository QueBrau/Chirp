"""POST /media/upload-url (board card c70).

No real GCS bucket exists yet — court's plan is bucket + IAM grants land as an infra
step once this route is ready to test against them. Everything provable without a live
bucket is tested here: auth, content-type/size validation (which run before any network
call and are 400s a real bucket could never influence), the 503-when-unconfigured fail-
closed path (mirrors stripe_service._secret_key exactly), and that a successful request
wires the GCS client correctly by faking the client/credentials boundary rather than the
route itself — the ROUTE's logic runs for real, only the network calls are doubles.

What is NOT provable here, on record rather than silently skipped: that the resulting
signed URL is one GCS actually accepts, and that the public_url is genuinely reachable
after a real PUT. That needs the real bucket and is the second verification pass court
already scoped for after the infra step lands.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.services import storage_service
from tests.conftest import MakeUser


@pytest.fixture(autouse=True)
def _reset_storage_client_cache():
    """storage_service._storage_client() memoizes a client in a module global — clear
    it between tests so one test's fake client can't leak into the next."""
    storage_service._client = None
    yield
    storage_service._client = None


def _fake_credentials() -> SimpleNamespace:
    """Stands in for google.auth.default()'s return value: has exactly the two
    attributes generate_upload_url() reads (service_account_email, token) plus a
    no-op refresh(), matching the real Credentials interface's shape."""
    return SimpleNamespace(
        service_account_email="chirp-api-run@chirps-prod.iam.gserviceaccount.com",
        token="fake-access-token",
        refresh=lambda request: None,
    )


def _fake_blob(captured: dict):
    class FakeBlob:
        def generate_signed_url(self, **kwargs):
            captured["signed_url_kwargs"] = kwargs
            return "https://storage.googleapis.com/fake-bucket/signed?sig=abc"

    return FakeBlob()


def _install_fake_gcs(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    """Patch the two real network boundaries generate_upload_url() crosses: the GCS
    client (bucket().blob()) and google.auth's credential/token machinery. Everything
    else in the function - validation, object naming, the returned SignedUpload shape -
    runs unmodified."""
    fake_bucket = SimpleNamespace(blob=lambda name: _fake_blob(captured))
    fake_client = SimpleNamespace(bucket=lambda name: fake_bucket)
    monkeypatch.setattr(storage_service, "_storage_client", lambda: fake_client)

    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda: (_fake_credentials(), "proj"))


async def test_returns_503_when_bucket_is_not_configured(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real state right now, since court's infra step hasn't landed: no bucket
    configured. Must fail closed with a clear reason, not error obscurely."""
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
    user = await make_user()
    response = await client.post(
        "/media/upload-url",
        json={"content_type": "image/jpeg", "byte_size": 1000},
        headers=user.headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "media_not_configured"


async def test_unauthenticated_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/media/upload-url", json={"content_type": "image/jpeg", "byte_size": 1000}
    )
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    "content_type",
    ["image/gif", "video/mp4", "application/pdf", "text/plain", "image/svg+xml"],
)
async def test_rejects_a_content_type_outside_the_alpha_allowlist(
    client: AsyncClient, make_user: MakeUser, content_type: str
) -> None:
    """JPEG/PNG/WebP only, per c70's alpha scope - explicitly including image/svg+xml
    and image/gif, since both LOOK like images and both are excluded on purpose (SVG
    can carry script content; GIF was never in scope)."""
    user = await make_user()
    response = await client.post(
        "/media/upload-url",
        json={"content_type": content_type, "byte_size": 1000},
        headers=user.headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "unsupported_content_type"


async def test_rejects_a_request_over_the_10mb_cap(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    response = await client.post(
        "/media/upload-url",
        json={"content_type": "image/jpeg", "byte_size": 10 * 1024 * 1024 + 1},
        headers=user.headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "file_too_large"


async def test_exactly_10mb_is_allowed(
    client: AsyncClient,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is inclusive - a photo landing on exactly the limit must not be a
    off-by-one rejection."""
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "chirps-prod-media")
    _install_fake_gcs(monkeypatch, {})
    user = await make_user()
    response = await client.post(
        "/media/upload-url",
        json={"content_type": "image/jpeg", "byte_size": 10 * 1024 * 1024},
        headers=user.headers,
    )
    assert response.status_code == 201, response.text


async def test_a_valid_request_returns_the_signed_and_public_urls(
    client: AsyncClient,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "chirps-prod-media")
    captured: dict = {}
    _install_fake_gcs(monkeypatch, captured)

    user = await make_user()
    response = await client.post(
        "/media/upload-url",
        json={"content_type": "image/png", "byte_size": 500_000},
        headers=user.headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["upload_url"] == "https://storage.googleapis.com/fake-bucket/signed?sig=abc"
    # public_url is computed directly, never round-tripped through the (faked) signer -
    # it must be the deterministic storage.googleapis.com form the docstring promises.
    assert body["public_url"].startswith("https://storage.googleapis.com/chirps-prod-media/posts/")
    assert body["public_url"].endswith(".png")
    assert body["expires_in_seconds"] == 15 * 60

    # The route actually asked GCS to sign a PUT with the right content-type and the
    # size-range header GCS enforces independently of the app-layer check above.
    signed_kwargs = captured["signed_url_kwargs"]
    assert signed_kwargs["method"] == "PUT"
    assert signed_kwargs["content_type"] == "image/png"
    assert signed_kwargs["headers"]["X-Goog-Content-Length-Range"] == "1,10485760"
    assert signed_kwargs["service_account_email"] == "chirp-api-run@chirps-prod.iam.gserviceaccount.com"


async def test_two_uploads_from_the_same_user_get_different_object_names(
    client: AsyncClient,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collision here would silently overwrite one user's earlier upload."""
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "chirps-prod-media")
    _install_fake_gcs(monkeypatch, {})
    user = await make_user()

    urls = []
    for _ in range(2):
        response = await client.post(
            "/media/upload-url",
            json={"content_type": "image/jpeg", "byte_size": 1000},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        urls.append(response.json()["public_url"])

    assert urls[0] != urls[1]
