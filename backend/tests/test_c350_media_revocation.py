"""Redirect-time media revocation (board card c350).

Before this card, GET /media/{token} verified only a capability token's HMAC signature
and expiry - being removed from a chapter, suspended, losing campus verification, or
having your post deleted had zero effect on a token someone already held, for up to its
full ~12h TTL. These tests pin the closed gap: the SAME already-minted token, re-fetched
after each kind of revocation, must now 403 rather than keep redirecting.

Every scenario test below does the mutation-then-refetch through real routes with a
token minted by a real POST /chapters/{id}/posts or /campuses/{id}/posts call (not a
hand-built token), and calls GET /media/{token} once BEFORE mutating state to prove the
baseline entitled path is actually exercised, not assumed. Each one then clears the
positive-decision memo (media_entitlement._reset_memo_for_tests) between the baseline
call and the post-mutation recheck - deliberately: the memo's own bounded grace period
is a separate, documented property with its own tests further down
(test_positive_entitlement_decision_is_memoized_then_denies_after_its_ttl and
test_denial_is_never_memoized_so_a_regrant_is_immediate), and conflating the two would
make a scenario test's result depend on wall-clock timing it has no business depending
on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import text

from app.services import media_entitlement, storage_service
from tests.conftest import MakeCampus, MakeChapterWith, MakeUser, set_campus

BUCKET = "chirps-c350-media"
SECRET = "c350-test-signing-secret"
BASE_URL = "https://chirp-api.example.run.app"
OBJECT = "posts/c350/photo.jpg"


@pytest.fixture(autouse=True)
def _media_signing_configured(monkeypatch: pytest.MonkeyPatch):
    """Turn signed reads on and clear every module-level cache between tests - same
    hygiene as test_media_signed_reads.py's identical fixture, plus the new
    entitlement memo this card adds."""
    settings = storage_service.get_settings()
    monkeypatch.setattr(settings, "media_bucket_name", BUCKET)
    monkeypatch.setattr(settings, "media_signing_secret", SECRET)
    monkeypatch.setattr(settings, "app_public_base_url", BASE_URL)
    storage_service._signed_read_cache.clear()
    storage_service._client = None
    media_entitlement._reset_memo_for_tests()
    yield
    storage_service._signed_read_cache.clear()
    storage_service._client = None
    media_entitlement._reset_memo_for_tests()


def _install_fake_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake only the GCS network boundary, same shape as test_media_signed_reads.py."""

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self._name = name

        def generate_signed_url(self, **kwargs):
            return f"https://storage.googleapis.com/{BUCKET}/{self._name}?sig=fake"

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


async def _grant_platform_admin(user_id: str) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


async def _insert_post_with_media(
    chapter_id: str, author_id: str, media_url: str, *, audience: str = "org"
) -> str:
    """Write a post row directly, bypassing the create route's GCS move - same shape
    and reasoning as test_media_signed_reads.py's private helper of the same name."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO posts (id, chapter_id, campus_id, author_id, body, media_urls, "
                "audience, post_type, created_at) "
                "SELECT gen_random_uuid(), :chapter, c.campus_id, :author, 'photo post', "
                "ARRAY[:media], :audience, 'photo', now() FROM chapters c WHERE c.id = :chapter "
                "RETURNING id"
            ),
            {"chapter": chapter_id, "author": author_id, "media": media_url, "audience": audience},
        )
        post_id = str(result.scalar_one())
        await session.commit()
    return post_id


async def _create_chapter_photo_post(
    client: AsyncClient,
    setup,
    monkeypatch: pytest.MonkeyPatch,
    *,
    audience: str = "org",
) -> tuple[str, str]:
    """POST a real photo post through the chapter route, GCS move faked. Returns
    (post_id, token)."""
    from app.routers import feed as feed_router

    _install_fake_signer(monkeypatch)
    permanent = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    monkeypatch.setattr(feed_router, "finalize_media_object", lambda user_id, name: permanent)
    response = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={
            "body": "a photo",
            "media_object_names": [f"tmp/{setup.member.id}/x.jpg"],
            "post_type": "photo",
            "audience": audience,
        },
        headers=setup.member.headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["id"], body["media_urls"][0].rsplit("/", 1)[1]


# ---------------------------------------------------------------------------
# R1: the revocation window is a live Settings value, not a frozen constant
# ---------------------------------------------------------------------------


def test_window_is_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falsification: red before c350 - MEDIA_TOKEN_WINDOW/MEDIA_TOKEN_TTL were
    module-level constants computed once at import, so changing the Settings
    attribute afterward had zero effect on a freshly minted token's expiry."""
    monkeypatch.setattr(storage_service.get_settings(), "media_revocation_window_hours", 1)
    viewer = str(uuid.uuid4())
    start = datetime(2026, 9, 11, 6, 0, 0, tzinfo=timezone.utc)
    token = storage_service.mint_media_token(OBJECT, viewer, now=start)

    # TTL is 2x the (now 1h) window == 2h, not the old hardcoded 12h.
    assert storage_service.verify_media_token(
        token, now=start + timedelta(hours=1, minutes=59)
    ) == (OBJECT, viewer)
    with pytest.raises(HTTPException) as exc:
        storage_service.verify_media_token(token, now=start + timedelta(hours=2, minutes=1))
    assert exc.value.status_code == 410


# ---------------------------------------------------------------------------
# R2: entitlement re-check on every GET /media/{token}
# ---------------------------------------------------------------------------


async def test_redirect_denies_after_chapter_removal(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsification: red before c350 - read_media never re-checked membership, so
    the identical token keeps 302-ing until its own ~12h expiry regardless of the
    PATCH."""
    setup = await make_chapter_with(role="member")
    _, token = await _create_chapter_photo_post(client, setup, monkeypatch)

    baseline = await client.get(f"/media/{token}")
    assert baseline.status_code == 302, baseline.text  # entitled state actually exercised

    removed = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "status": "removed"},
        headers=setup.president.headers,
    )
    assert removed.status_code == 200, removed.text

    media_entitlement._reset_memo_for_tests()
    revoked = await client.get(f"/media/{token}")
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "media_access_revoked"


async def test_redirect_denies_after_suspension(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falsification: red before c350, same reason as chapter removal - no DB check
    exists at redirect time at all."""
    setup = await make_chapter_with(role="member")
    _, token = await _create_chapter_photo_post(client, setup, monkeypatch)

    baseline = await client.get(f"/media/{token}")
    assert baseline.status_code == 302, baseline.text

    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)
    suspend = await client.post(
        f"/moderation/users/{setup.member.id}/suspend",
        json={"reason": "test suspension"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text

    media_entitlement._reset_memo_for_tests()
    revoked = await client.get(f"/media/{token}")
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "media_access_revoked"


async def test_redirect_denies_after_post_deletion(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsification: red before c350 - delete_post/delete_own_post only ever set
    deleted_at and never touch storage_service, so the token kept 302-ing to an
    object whose owning post no longer existed."""
    setup = await make_chapter_with(role="member")
    post_id, token = await _create_chapter_photo_post(client, setup, monkeypatch)

    baseline = await client.get(f"/media/{token}")
    assert baseline.status_code == 302, baseline.text

    deleted = await client.delete(f"/posts/{post_id}", headers=setup.member.headers)
    assert deleted.status_code == 204, deleted.text

    media_entitlement._reset_memo_for_tests()
    revoked = await client.get(f"/media/{token}")
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "media_access_revoked"


async def test_org_actives_denies_inactive_viewer(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinguishes the actives-only tier from plain 'org': the viewer's status is
    flipped to 'inactive' - NOT 'removed' - so they remain a valid chapter member who
    would still pass the plain 'org' branch. Falsification: red before c350 (no
    check at all); would ALSO be a false green against an implementation that only
    checked membership existence instead of reusing _visible_audiences' active-only
    rule for this tier specifically."""
    setup = await make_chapter_with(role="member")
    _, token = await _create_chapter_photo_post(client, setup, monkeypatch, audience="org_actives")

    baseline = await client.get(f"/media/{token}")
    assert baseline.status_code == 302, baseline.text

    inactive = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "status": "inactive"},
        headers=setup.president.headers,
    )
    assert inactive.status_code == 200, inactive.text

    media_entitlement._reset_memo_for_tests()
    revoked = await client.get(f"/media/{token}")
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "media_access_revoked"


async def test_campus_post_denies_unverified_viewer(
    client: AsyncClient,
    make_user: MakeUser,
    make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the campus branch specifically - a separate code path from the
    chapter-membership branch above, so a naive implementation covering only
    chapters would leave this one red. Falsification: red before c350 (no check at
    all)."""
    from app.db import get_session_factory
    from app.routers import feed as feed_router

    _install_fake_signer(monkeypatch)
    user = await make_user("Campus Poster")
    campus_id = await make_campus()
    await set_campus(user.id, campus_id, verified=True)
    permanent = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    monkeypatch.setattr(feed_router, "finalize_media_object", lambda user_id, name: permanent)

    created = await client.post(
        f"/campuses/{campus_id}/posts",
        json={
            "body": "campus photo",
            "media_object_names": [f"tmp/{user.id}/x.jpg"],
            "post_type": "photo",
        },
        headers=user.headers,
    )
    assert created.status_code == 201, created.text
    token = created.json()["media_urls"][0].rsplit("/", 1)[1]

    baseline = await client.get(f"/media/{token}")
    assert baseline.status_code == 302, baseline.text

    # Lapse verification directly - no API un-verifies a campus; same raw-SQL
    # pattern conftest's own set_campus/verify_campus use for a state no client
    # action produces.
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET campus_verified_at = NULL WHERE id = :id"),
            {"id": user.id},
        )
        await session.commit()

    media_entitlement._reset_memo_for_tests()
    revoked = await client.get(f"/media/{token}")
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "media_access_revoked"


async def test_object_with_no_owning_post_denies_cleanly(client: AsyncClient) -> None:
    """The empty-reverse-lookup edge case: a well-formed, correctly-signed token
    whose object_name matches NO row in posts.media_urls at all (synthetic/orphaned
    object, no post ever created). Falsification: red before c350 - an orphaned
    token 302s exactly like a real one, since nothing checks the database at all.
    Also the one case a naive implementation could get backwards in the other
    direction: 'no post found' must mean deny, never 'no restriction, allow'."""
    orphan_viewer = str(uuid.uuid4())
    token = storage_service.mint_media_token("posts/orphan/none.jpg", orphan_viewer)
    response = await client.get(f"/media/{token}")
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "media_access_revoked"


# ---------------------------------------------------------------------------
# R2/R3: Cache-Control
# ---------------------------------------------------------------------------


async def test_redirect_cache_control_is_no_store(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsification: red before c350 - the header used to read
    `private, max-age=<up to 21600>`."""
    setup = await make_chapter_with(role="member")
    _, token = await _create_chapter_photo_post(client, setup, monkeypatch)

    response = await client.get(f"/media/{token}")
    assert response.status_code == 302, response.text
    assert response.headers["cache-control"] == "private, no-store"


# ---------------------------------------------------------------------------
# R4: account-switch cache isolation rests on per-viewer capability URLs
# ---------------------------------------------------------------------------


def test_capability_urls_differ_per_viewer_and_each_verifies_to_its_own_viewer() -> None:
    """The PROPERTY under test here is pre-existing (verified_claims in the c350
    plan: viewer_id was already part of the signed payload before this card, and
    url_a != url_b already held on origin/main), not new behavior c350 adds. Per
    manager ruling R4, it is pinned here explicitly as the reason c350 carries NO
    mobile change: two viewers of the same photo already get different app URLs, so
    a native URL-keyed image cache is already isolated per account by construction.
    See SPEC.md's media section for the recorded rationale.

    THE TEST AS WRITTEN still falsifies red before c350, though - not because the
    isolation property is new, but because verify_media_token's return shape is
    (the tuple-equality assertions below fail against a bare object_name string on
    origin/main). Recorded honestly rather than claimed as a pre-existing-green
    test: see the build report's falsification section."""
    viewer_a = str(uuid.uuid4())
    viewer_b = str(uuid.uuid4())
    url_a = storage_service.media_capability_url(OBJECT, viewer_a)
    url_b = storage_service.media_capability_url(OBJECT, viewer_b)
    assert url_a != url_b

    token_a = url_a.rsplit("/", 1)[1]
    token_b = url_b.rsplit("/", 1)[1]
    assert storage_service.verify_media_token(token_a) == (OBJECT, viewer_a)
    assert storage_service.verify_media_token(token_b) == (OBJECT, viewer_b)


# ---------------------------------------------------------------------------
# R5: the positive-entitlement memo and its bounded revocation floor
# ---------------------------------------------------------------------------


async def test_positive_entitlement_decision_is_memoized_then_denies_after_its_ttl(
    make_chapter_with: MakeChapterWith,
) -> None:
    """The memo's whole point: a feed render fanning out to 20+ media GETs must not
    hit the database on every one, so a POSITIVE decision is reused for
    media_entitlement_memo_seconds (60s default). That means a revocation's real
    effect can legitimately lag by up to that long for a viewer who already holds a
    memoized decision - proven here directly against the entitlement function with
    controlled `now`, the same pattern storage_service's own memoized signed_read_url
    tests use for exactly this reason (real time cannot be made to advance 60s inside
    a test). This is additive new behavior (media_entitlement.py did not exist before
    c350), so there is no prior-code red state to show for the MEMO itself; the
    meaningful falsification for the underlying check is the scenario tests above,
    run against a reverted media.py/storage_service.py - see the build report."""
    from app.db import get_session_factory

    setup = await make_chapter_with(role="member")
    permanent = f"https://storage.googleapis.com/{BUCKET}/{OBJECT}"
    await _insert_post_with_media(setup.chapter_id, setup.member.id, permanent)
    t0 = datetime(2026, 9, 11, 6, 0, 0, tzinfo=timezone.utc)

    async with get_session_factory()() as session:
        first = await media_entitlement.check_media_entitlement(
            session, OBJECT, str(setup.member.id), now=t0
        )
    assert first is True  # a real, entitled decision - not a default

    # Revoke WITHOUT clearing the memo - the direct-DB mutation below would make a
    # fresh check deny immediately; the memo is what keeps the OLD positive answer
    # alive for a while longer.
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE memberships SET status = 'removed' "
                "WHERE chapter_id = :chapter AND user_id = :user"
            ),
            {"chapter": setup.chapter_id, "user": setup.member.id},
        )
        await session.commit()

    async with get_session_factory()() as session:
        within_ttl = await media_entitlement.check_media_entitlement(
            session, OBJECT, str(setup.member.id), now=t0 + timedelta(seconds=30)
        )
    assert within_ttl is True, "memo hit expected: 30s is inside the 60s default TTL"

    async with get_session_factory()() as session:
        after_ttl = await media_entitlement.check_media_entitlement(
            session, OBJECT, str(setup.member.id), now=t0 + timedelta(seconds=61)
        )
    assert after_ttl is False, "memo entry must have expired by 61s and hit the real (denied) DB state"


async def test_denial_is_never_memoized_so_a_regrant_is_immediate(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flip side of the memo rule: a DENIAL is never cached, so reversing a
    mistaken removal is visible on the very next request, with no wait. Falsification:
    a naive 'memoize every decision' implementation would leave this red - the
    re-grant would still 403 for up to media_entitlement_memo_seconds after the
    denial was cached.

    TWO denied checks precede the re-grant, not one - this is deliberate and found
    by this build's own sabotage matrix: a mutant that memoizes a DENIAL's key with
    an expiry (indistinguishable, in a memo that stores only "positive until X", from
    memoizing an ALLOW) makes the FIRST denied-then-regranted-then-allowed sequence
    pass by coincidence - the mis-memoized "allow" from the denial happens to agree
    with the real answer once regranted. A second still-revoked check in between is
    what actually discriminates: under that same mutant it would incorrectly return
    302 (the bad memo entry, not a fresh decision), where the real code re-evaluates
    and correctly still denies."""
    setup = await make_chapter_with(role="member")
    _, token = await _create_chapter_photo_post(client, setup, monkeypatch)

    removed = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "status": "removed"},
        headers=setup.president.headers,
    )
    assert removed.status_code == 200, removed.text

    denied = await client.get(f"/media/{token}")
    assert denied.status_code == 403, denied.text  # first entitlement check for this
    # token IS the denial - nothing was memoized positively before this point.

    still_denied = await client.get(f"/media/{token}")
    assert still_denied.status_code == 403, still_denied.text  # still revoked, and a
    # correct implementation re-decides rather than trusting anything cached from the
    # prior denial.

    regranted = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "status": "active"},
        headers=setup.president.headers,
    )
    assert regranted.status_code == 200, regranted.text

    allowed_again = await client.get(f"/media/{token}")
    assert allowed_again.status_code == 302, allowed_again.text
