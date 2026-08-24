"""Capability-url media reads: token, normalizer, route, memo (board card c140).

c140's finding: a post's photo was world-readable forever by url, including on
audience='org' posts the schema documents as "private to a chapter". The fix keeps the
bucket private and serializes an app-owned, expiring capability url instead of the
stored public one.

WHAT THESE TESTS PIN, and why each one is here rather than assumed:

- The QUANTIZED-EXPIRY property, which is the whole reason this design is viable. Two
  mints inside one window must be BYTE-IDENTICAL, because React Native's Image keys its
  cache on the url string - a url that churns per request is a cache key that churns per
  request, and every feed load would re-download every photo. This is the one test that,
  if it ever goes red, means the feature still "works" while being quietly ruinous. It
  cannot be caught by hand-testing.
- The THREE-BRANCH normalizer, including the legacy shapes. media_urls has held three
  different url shapes over time (unvalidated client input pre-c139, bucket-root-validated
  client input c139->c132, server-assigned since c132), so "strip the canonical prefix"
  is not sufficient. The percent-encoded JSON-API form is tested with an ENCODED fixture
  on purpose: an unencoded one would pass against a shape prod could never contain and
  miss the shape it actually would.
- That a valid SIGNATURE is not accepted as a valid PATH.

NOT provable here, on record rather than silently skipped: that the GCS signed url behind
the redirect is one GCS actually honours, and that a real device follows the 302 and
caches against the request url. The first needs the real bucket; the second was settled
from the installed React Native source (RCTImageLoader.mm:551 lookup, :870-871 store,
RCTHTTPRequestHandler.mm:135-146 redirect) and rides c39's EAS unblock for a device pass.
Note for whoever does that pass: Expo WEB proves nothing here - on web RN's Image is an
<img> and caching is the browser's, not RCTImageCache or Fresco.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.services import storage_service
from tests.conftest import MakeChapterWith, MakeUser

BUCKET = "chirps-prod-media"
SECRET = "test-media-signing-secret"
BASE_URL = "https://chirp-api.example.run.app"
OBJECT = "posts/2b0f/deadbeef.jpg"
VIEWER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _media_signing_configured(monkeypatch: pytest.MonkeyPatch):
    """Turn signed reads on, and clear both module-level caches between tests.

    _signed_read_cache is a process-wide dict; leaking it across tests would let one
    test's fake signed url satisfy another test's memo assertions.
    """
    settings = storage_service.get_settings()
    monkeypatch.setattr(settings, "media_bucket_name", BUCKET)
    monkeypatch.setattr(settings, "media_signing_secret", SECRET)
    monkeypatch.setattr(settings, "app_public_base_url", BASE_URL)
    storage_service._signed_read_cache.clear()
    storage_service._client = None
    yield
    storage_service._signed_read_cache.clear()
    storage_service._client = None


def _install_fake_signer(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """Fake only the GCS network boundary; mint/verify/memo logic runs for real."""

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self._name = name

        def generate_signed_url(self, **kwargs):
            calls.append((self._name, kwargs))
            return f"https://storage.googleapis.com/{BUCKET}/{self._name}?sig={len(calls)}"

    fake_bucket = SimpleNamespace(blob=lambda name: FakeBlob(name))
    monkeypatch.setattr(
        storage_service, "_storage_client", lambda: SimpleNamespace(bucket=lambda n: fake_bucket)
    )

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


# ---------------------------------------------------------------------------
# Quantized expiry - the property the whole design rests on
# ---------------------------------------------------------------------------


def test_two_mints_in_the_same_window_are_byte_identical() -> None:
    """THE cache-stability property. If this breaks, every feed load re-downloads every
    photo on every device, and nothing else fails visibly - the urls still work."""
    start = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)
    first = storage_service.mint_media_token(OBJECT, VIEWER, now=start)
    later = storage_service.mint_media_token(
        OBJECT, VIEWER, now=start + timedelta(hours=5, minutes=59)
    )
    assert first == later


def test_mints_in_different_windows_differ() -> None:
    """The flip side: the token must actually roll over, or it would never expire."""
    start = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)
    first = storage_service.mint_media_token(OBJECT, VIEWER, now=start)
    next_window = storage_service.mint_media_token(OBJECT, VIEWER, now=start + timedelta(hours=6))
    assert first != next_window


def test_different_viewers_get_different_tokens() -> None:
    now = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)
    mine = storage_service.mint_media_token(OBJECT, VIEWER, now=now)
    theirs = storage_service.mint_media_token(OBJECT, "22222222-2222-2222-2222-222222222222", now=now)
    assert mine != theirs


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def test_round_trip_returns_the_object_name() -> None:
    token = storage_service.mint_media_token(OBJECT, VIEWER)
    assert storage_service.verify_media_token(token) == OBJECT


def test_a_tampered_payload_is_rejected() -> None:
    """Flipping the object name in the payload must not verify - otherwise the token is
    a suggestion rather than a capability."""
    token = storage_service.mint_media_token(OBJECT, VIEWER)
    payload, signature = token.split(".", 1)
    forged = storage_service._b64(b"posts/other/secret.jpg\x00" + VIEWER.encode() + b"\x009999999999")
    with pytest.raises(HTTPException) as exc:
        storage_service.verify_media_token(f"{forged}.{signature}")
    assert exc.value.status_code == 403
    assert exc.value.detail == "invalid_media_token"


def test_a_token_signed_with_another_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = storage_service.mint_media_token(OBJECT, VIEWER)
    monkeypatch.setattr(storage_service.get_settings(), "media_signing_secret", "a-different-secret")
    with pytest.raises(HTTPException) as exc:
        storage_service.verify_media_token(token)
    assert exc.value.status_code == 403


def test_a_malformed_token_is_rejected_rather_than_crashing() -> None:
    for junk in ["", "no-dot", "!!!.!!!", "a.b.c"]:
        with pytest.raises(HTTPException) as exc:
            storage_service.verify_media_token(junk)
        assert exc.value.status_code == 403, junk


def test_an_expired_token_is_410_not_403() -> None:
    """410 and 403 mean operationally different things here: 410 is a genuine url that
    aged out (refetch the feed), 403 is a token someone edited. Collapsing them would
    make a routine lifecycle event indistinguishable from an attack in the logs."""
    minted_at = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)
    token = storage_service.mint_media_token(OBJECT, VIEWER, now=minted_at)
    with pytest.raises(HTTPException) as exc:
        storage_service.verify_media_token(token, now=minted_at + timedelta(hours=13))
    assert exc.value.status_code == 410
    assert exc.value.detail == "media_token_expired"


def test_a_token_is_still_valid_just_before_its_expiry() -> None:
    """TTL is 2x the window, so a token minted at a window START has 12h of life."""
    minted_at = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)
    token = storage_service.mint_media_token(OBJECT, VIEWER, now=minted_at)
    assert storage_service.verify_media_token(
        token, now=minted_at + timedelta(hours=11, minutes=59)
    ) == OBJECT


def test_a_validly_signed_token_for_a_tmp_path_is_still_refused() -> None:
    """A valid signature proves WE minted it, not that what we minted is servable. tmp/
    holds provisional uploads nobody has attached to a post; this route must never
    redirect to one even if a bug elsewhere minted the token."""
    token = storage_service.mint_media_token(f"tmp/{VIEWER}/abc.jpg", VIEWER)
    with pytest.raises(HTTPException) as exc:
        storage_service.verify_media_token(token)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# The three-branch stored-url normalizer
# ---------------------------------------------------------------------------


def test_canonical_url_maps_to_its_object_name() -> None:
    url = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    assert storage_service.object_name_from_stored_url(url) == OBJECT


def test_console_download_form_maps_to_the_same_object_name() -> None:
    """Legacy alternate form. Path arrives UNENCODED here, unlike the JSON API form."""
    url = f"https://storage.cloud.google.com/{BUCKET}/{OBJECT}"
    assert storage_service.object_name_from_stored_url(url) == OBJECT


def test_json_api_form_percent_decodes_the_object_name() -> None:
    """The realistic encoded fixture: a JSON-API url carries the object name percent-
    encoded, so the slashes arrive as %2F. Testing this with an UNENCODED path would
    pass against a shape prod could never contain and miss the one it would."""
    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/posts%2F2b0f%2Fdeadbeef.jpg"
    assert storage_service.object_name_from_stored_url(url) == OBJECT


def test_json_api_form_decodes_exactly_once() -> None:
    """Double-decoding silently corrupts any name containing an encoded percent. The
    object name here genuinely contains the four characters '%2F'; encoded once that is
    '%252F'. One unquote returns it intact, two would turn it into a path separator and
    point us at a different object entirely."""
    object_with_percent = "posts/2b0f/a%2Fb.jpg"
    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/posts%2F2b0f%2Fa%252Fb.jpg"
    assert storage_service.object_name_from_stored_url(url) == object_with_percent


def test_json_api_form_is_matched_before_the_canonical_form() -> None:
    """Both start with the same host. If the canonical branch ran first, a bucket named
    'storage' would make it mis-slice a JSON-API url into a garbage object name."""
    url = "https://storage.googleapis.com/storage/v1/b/storage/o/posts%2Fx%2Fy.jpg"
    storage_service.get_settings().media_bucket_name = "storage"
    try:
        assert storage_service.object_name_from_stored_url(url) == "posts/x/y.jpg"
    finally:
        storage_service.get_settings().media_bucket_name = BUCKET


def test_a_foreign_host_is_not_ours_to_sign() -> None:
    """Only reachable on pre-c139 rows, when media_urls was unvalidated client input.
    None means the caller passes it through untouched - making OUR bucket private has no
    effect on someone else's host."""
    assert storage_service.object_name_from_stored_url("https://evil.example.com/x.jpg") is None


def test_another_bucket_on_the_same_host_is_not_ours_either() -> None:
    assert (
        storage_service.object_name_from_stored_url(
            "https://storage.googleapis.com/some-other-bucket/posts/x.jpg"
        )
        is None
    )


def test_query_parameters_are_stripped_from_a_stored_url() -> None:
    url = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}?generation=17"
    assert storage_service.object_name_from_stored_url(url) == OBJECT


def test_no_configured_bucket_means_nothing_is_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)
    url = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    assert storage_service.object_name_from_stored_url(url) is None


# ---------------------------------------------------------------------------
# The memo behind the redirect
# ---------------------------------------------------------------------------


def test_the_signed_url_is_memoized_within_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memo is load-bearing twice: it keeps an IAM signBlob round trip off every
    image request, AND it keeps the redirect TARGET stable, which is what protects
    caching on any platform that might key on the final url rather than the request url.
    Do not let this become an optional optimization."""
    calls: list = []
    _install_fake_signer(monkeypatch, calls)
    now = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)

    first = storage_service.signed_read_url(OBJECT, now=now)
    second = storage_service.signed_read_url(OBJECT, now=now + timedelta(hours=5))
    assert first == second
    assert len(calls) == 1, "signed a second time inside one window"


def test_a_new_window_signs_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _install_fake_signer(monkeypatch, calls)
    now = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)

    storage_service.signed_read_url(OBJECT, now=now)
    storage_service.signed_read_url(OBJECT, now=now + timedelta(hours=6))
    assert len(calls) == 2


def test_two_objects_in_one_window_do_not_evict_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eviction is by WINDOW, not by size - a feed showing many photos must not thrash."""
    calls: list = []
    _install_fake_signer(monkeypatch, calls)
    now = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)

    storage_service.signed_read_url("posts/a/1.jpg", now=now)
    storage_service.signed_read_url("posts/b/2.jpg", now=now)
    storage_service.signed_read_url("posts/a/1.jpg", now=now)
    assert len(calls) == 2


def test_the_signed_read_is_a_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _install_fake_signer(monkeypatch, calls)
    storage_service.signed_read_url(OBJECT)
    _name, kwargs = calls[0]
    assert kwargs["method"] == "GET"
    assert kwargs["version"] == "v4"


# ---------------------------------------------------------------------------
# GET /media/{token}
# ---------------------------------------------------------------------------


async def test_the_route_redirects_to_the_signed_url(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    _install_fake_signer(monkeypatch, calls)
    token = storage_service.mint_media_token(OBJECT, VIEWER)

    response = await client.get(f"/media/{token}")
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(
        f"https://storage.googleapis.com/{BUCKET}/{OBJECT}?sig="
    )
    assert "private" in response.headers["cache-control"]


async def test_the_route_needs_no_auth_header(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberate and load-bearing: RN's Image cannot send an Authorization header, so a
    route images are fetched from cannot authenticate its caller. Adding an auth
    dependency here would break every photo in the app. The token IS the capability."""
    _install_fake_signer(monkeypatch, [])
    token = storage_service.mint_media_token(OBJECT, VIEWER)
    response = await client.get(f"/media/{token}")
    assert response.status_code == 302, response.text


async def test_the_route_rejects_a_forged_token(client: AsyncClient) -> None:
    response = await client.get("/media/not-a-real-token")
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "invalid_media_token"


async def test_the_route_reports_expiry_as_410(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_signer(monkeypatch, [])
    stale = storage_service.mint_media_token(
        OBJECT, VIEWER, now=datetime.now(timezone.utc) - timedelta(hours=24)
    )
    response = await client.get(f"/media/{stale}")
    assert response.status_code == 410, response.text
    assert response.json()["detail"] == "media_token_expired"


# ---------------------------------------------------------------------------
# Serialization into the feed
# ---------------------------------------------------------------------------


async def test_a_feed_post_serves_a_capability_url_not_the_stored_one(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real route: the STORED value stays canonical and the
    SERVED value is a capability url. Both halves matter - c153's reconciliation job
    diffs bucket objects against the stored column."""
    from app import models
    from app.routers import feed as feed_router

    _install_fake_signer(monkeypatch, [])
    user = await make_user()
    stored = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"

    serialized = feed_router._serialize_media_urls([stored], user.id)
    assert serialized is not None
    assert serialized[0].startswith(f"{BASE_URL}/media/")
    assert stored not in serialized[0]
    assert models.Post is not None  # import guard: the model module actually loaded

    # and the capability url resolves back to the same object
    token = serialized[0].rsplit("/", 1)[1]
    assert storage_service.verify_media_token(token) == OBJECT


async def test_serialization_falls_through_when_signing_is_not_configured(
    make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state every deployment is in until the c140 cutover. This is what makes the
    change additive - it flips nothing on its own."""
    from app.routers import feed as feed_router

    monkeypatch.setattr(storage_service.get_settings(), "media_signing_secret", None)
    user = await make_user()
    stored = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    assert feed_router._serialize_media_urls([stored], user.id) == [stored]


async def test_a_foreign_url_is_served_unchanged(
    make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-c139 rows only. Not ours to sign, and unaffected by our bucket going private."""
    from app.routers import feed as feed_router

    user = await make_user()
    foreign = "https://picsum.photos/seed/x/800/600"
    assert feed_router._serialize_media_urls([foreign], user.id) == [foreign]


async def test_a_legacy_alternate_form_row_is_signed_rather_than_passed_through(
    make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch that only exists because of the alternate-form case. A console-form url
    references a REAL object in our bucket, so passing it through would leave it pointing
    at a bucket about to stop answering anonymous requests - a broken photo, not a
    preserved one."""
    from app.routers import feed as feed_router

    user = await make_user()
    legacy = f"https://storage.cloud.google.com/{BUCKET}/{OBJECT}"
    serialized = feed_router._serialize_media_urls([legacy], user.id)
    assert serialized is not None
    assert serialized[0].startswith(f"{BASE_URL}/media/")
    token = serialized[0].rsplit("/", 1)[1]
    assert storage_service.verify_media_token(token) == OBJECT


async def test_a_post_with_no_media_is_untouched(make_user: MakeUser) -> None:
    from app.routers import feed as feed_router

    user = await make_user()
    assert feed_router._serialize_media_urls(None, user.id) is None
    assert feed_router._serialize_media_urls([], user.id) == []


# ---------------------------------------------------------------------------
# End-to-end wiring through the real routes
#
# The serializer tests above call _serialize_media_urls directly, which proves the
# TRANSFORM but not that it is actually plumbed into the routes that serve posts. Those
# are separate failures: a correct transform nobody calls ships the exact bug this card
# exists to fix, and every pre-existing feed test would still pass, because they all run
# with signing unconfigured and therefore exercise only the fall-through branch.
# ---------------------------------------------------------------------------


async def _insert_post_with_media(chapter_id: str, author_id: str, media_url: str) -> str:
    """Write a post row directly, bypassing the create route's GCS move.

    Raw SQL for the same reason conftest.set_campus uses it: this is setting up a state
    the API deliberately will not let a client ask for (media_urls is server-assigned),
    and the point here is to test the READ path against a row that already exists.
    """
    from sqlalchemy import text

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO posts (id, chapter_id, campus_id, author_id, body, media_urls, "
                "audience, post_type, created_at) "
                "SELECT gen_random_uuid(), :chapter, c.campus_id, :author, 'photo post', "
                "ARRAY[:media], 'org', 'photo', now() FROM chapters c WHERE c.id = :chapter "
                "RETURNING id"
            ),
            {"chapter": chapter_id, "author": author_id, "media": media_url},
        )
        post_id = str(result.scalar_one())
        await session.commit()
    return post_id


async def test_listing_a_chapter_feed_serves_capability_urls(
    client: AsyncClient,
    make_chapter_with: "MakeChapterWith",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /chapters/{id}/posts is the org-audience read path - the exact route whose
    photos c140 found to be world-readable. Proves _feed_post_out is wired, not just
    that the transform works in isolation."""
    _install_fake_signer(monkeypatch, [])
    setup = await make_chapter_with(role="member")
    stored = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    await _insert_post_with_media(setup.chapter_id, setup.member.id, stored)

    response = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    served = response.json()[0]["media_urls"][0]
    assert served.startswith(f"{BASE_URL}/media/")
    assert stored not in served
    assert storage_service.verify_media_token(served.rsplit("/", 1)[1]) == OBJECT


async def test_the_stored_column_is_not_rewritten_by_serving_it(
    client: AsyncClient,
    make_chapter_with: "MakeChapterWith",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant c153's reconciliation job depends on: serving a post must not touch
    posts.media_urls. If this ever regresses, that job starts diffing bucket objects
    against capability urls and concludes every real photo is unreferenced."""
    from sqlalchemy import text

    from app.db import get_session_factory

    _install_fake_signer(monkeypatch, [])
    setup = await make_chapter_with(role="member")
    stored = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    post_id = await _insert_post_with_media(setup.chapter_id, setup.member.id, stored)

    await client.get(f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers)

    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT media_urls FROM posts WHERE id = :id"), {"id": post_id}
        )
        assert result.scalar_one() == [stored]


async def test_creating_a_post_returns_a_capability_url(
    client: AsyncClient,
    make_chapter_with: "MakeChapterWith",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /chapters/{id}/posts returns PostOut, a different serialization site from the
    feed list - it needed its own wiring and therefore its own test."""
    from app.routers import feed as feed_router

    _install_fake_signer(monkeypatch, [])
    setup = await make_chapter_with(role="member")
    permanent = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    # Only the GCS move is faked; validate_media_object_names and validate_media_urls
    # both still run for real on either side of it.
    monkeypatch.setattr(feed_router, "finalize_media_object", lambda user_id, name: permanent)

    response = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={
            "body": "photo post",
            "media_object_names": [f"tmp/{setup.member.id}/abc.jpg"],
            "post_type": "photo",
        },
        headers=setup.member.headers,
    )
    assert response.status_code == 201, response.text
    served = response.json()["media_urls"][0]
    assert served.startswith(f"{BASE_URL}/media/")
    assert storage_service.verify_media_token(served.rsplit("/", 1)[1]) == OBJECT
