"""c259: every abuse-prone write path is rate limited, and none of them fire on real use.

Before this, services/rate_limit.py was called in exactly TWO places in the whole
backend. Posting, commenting, chirping, messaging, invites, reports, account creation
and signed media urls had nothing at all, so a single authenticated account could loop
any of them as fast as the network allowed.

EVERY LIMIT IS PROVEN BOTH WAYS HERE, and the second way is the one that matters:

  1. a loop is stopped — the call past the ceiling returns 429 with a
     machine-readable `<scope>_rate_limited` detail, so a client can tell WHICH limit
     it hit rather than getting an opaque 429;
  2. everything below the ceiling is NOT rate limited, and the documented
     heavy-real-user pattern sits far enough under it to be checked by assertion.

That second half is the whole risk of this card. A limiter that fires on ordinary use
is worse than no limiter: at alpha it is indistinguishable from the app being broken,
and it arrives as "Chirp wouldn't let me post", which is not a bug report anyone can
act on. So each test states the realistic pattern it is protecting and asserts the
headroom, rather than trusting the number in the constant.

Note the limits are enforced as ROUTE DEPENDENCIES rather than inside handlers, so a
refused request never reaches the handler — which is what makes the invite-redemption
limit a code-guessing control too, since a wrong code costs an attempt either way.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.core.rate_limits import (
    ACCOUNT_BOOTSTRAP_LIMIT,
    CHIRP_CREATE_LIMIT,
    COMMENT_CREATE_LIMIT,
    INVITE_MINT_LIMIT,
    INVITE_REDEEM_LIMIT,
    MEDIA_UPLOAD_URL_LIMIT,
    MESSAGE_SEND_LIMIT,
    POST_CREATE_LIMIT,
    REPORT_CREATE_LIMIT,
)
from app.services import storage_service
from tests.conftest import (
    ApiUser,
    MakeCampus,
    MakeChapterWith,
    MakeUser,
    RegisterDevice,
    b64,
    set_campus,
    share_verified_campus,
)
from tests.test_media_upload_url import _install_fake_gcs


@pytest.fixture(autouse=True)
def _reset_storage_client_cache():
    storage_service._client = None
    yield
    storage_service._client = None


def _assert_headroom(realistic: int, limit: tuple[int, int], label: str) -> None:
    """The busiest plausible real pattern must sit well under the ceiling.

    Not decoration: this is the assertion that fails if someone tightens a limit
    toward what a real user actually does. Two times is the floor — below that a
    slightly-busier-than-usual day starts hitting 429s.
    """
    max_calls, _window = limit
    assert realistic * 2 <= max_calls, (
        f"{label}: ceiling {max_calls} is not comfortably above the realistic "
        f"heavy pattern of {realistic}"
    )


async def _make_campus_user(
    client: AsyncClient, campus_id: str, name: str = "Rate Limited"
) -> ApiUser:
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={"email": email, "display_name": name, "account_type": "non_greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    await set_campus(user.id, campus_id)
    return user


# ---------------------------------------------------------------------------
# 1. media upload urls — real storage, real money, an IAM round trip per call
# ---------------------------------------------------------------------------


async def test_media_upload_url_stops_a_loop_but_not_a_photo_carousel(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heavy real pattern: a 10-photo carousel with re-picks, two or three posts back
    to back while composing — call it 30 mints. The ceiling is double that."""
    _assert_headroom(30, MEDIA_UPLOAD_URL_LIMIT, "media_upload_url")
    max_calls, _ = MEDIA_UPLOAD_URL_LIMIT

    monkeypatch.setattr(
        storage_service.get_settings(), "media_bucket_name", "chirps-prod-media"
    )
    _install_fake_gcs(monkeypatch, {})
    user = await make_user()
    body = {"content_type": "image/jpeg", "byte_size": 1000}

    for i in range(max_calls):
        response = await client.post("/media/upload-url", json=body, headers=user.headers)
        assert response.status_code == 201, f"mint {i + 1} of {max_calls}: {response.text}"

    refused = await client.post("/media/upload-url", json=body, headers=user.headers)
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "media_upload_url_rate_limited"


async def test_media_upload_url_limit_is_per_user_not_global(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One account exhausting its budget must not lock everyone else out — a global
    limiter on a shared endpoint is a denial-of-service handed to any single user."""
    monkeypatch.setattr(
        storage_service.get_settings(), "media_bucket_name", "chirps-prod-media"
    )
    _install_fake_gcs(monkeypatch, {})
    max_calls, _ = MEDIA_UPLOAD_URL_LIMIT
    body = {"content_type": "image/jpeg", "byte_size": 1000}

    hog = await make_user("Hog")
    for _ in range(max_calls):
        assert (
            await client.post("/media/upload-url", json=body, headers=hog.headers)
        ).status_code == 201
    assert (
        await client.post("/media/upload-url", json=body, headers=hog.headers)
    ).status_code == 429

    bystander = await make_user("Bystander")
    response = await client.post(
        "/media/upload-url", json=body, headers=bystander.headers
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# 2. account creation — the farming vector, and the only per-IP limit
# ---------------------------------------------------------------------------


async def test_account_bootstrap_is_limited_per_ip(client: AsyncClient) -> None:
    """Every request in this suite shares one client address, which is exactly the
    shape of a NAT'd campus — so this test doubles as the check that the limit is
    reached by DIFFERENT identities from one address, not by one identity repeating."""
    max_calls, _ = ACCOUNT_BOOTSTRAP_LIMIT

    for i in range(max_calls):
        uid = f"uid-{uuid.uuid4().hex}"
        response = await client.post(
            "/auth/bootstrap",
            json={
                "email": f"{uid}@example.edu",
                "display_name": "Fresh Signup",
                "account_type": "greek",
            },
            headers={"X-Debug-Firebase-Uid": uid},
        )
        assert response.status_code == 201, f"signup {i + 1}: {response.text}"

    uid = f"uid-{uuid.uuid4().hex}"
    refused = await client.post(
        "/auth/bootstrap",
        json={
            "email": f"{uid}@example.edu",
            "display_name": "One Too Many",
            "account_type": "greek",
        },
        headers={"X-Debug-Firebase-Uid": uid},
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "account_bootstrap_rate_limited"


async def test_a_different_ip_has_its_own_bootstrap_budget(client: AsyncClient) -> None:
    """X-Forwarded-For is what identifies the caller behind Cloud Run's front end, so
    a second address must not inherit the first one's exhausted budget."""
    max_calls, _ = ACCOUNT_BOOTSTRAP_LIMIT

    for _ in range(max_calls):
        uid = f"uid-{uuid.uuid4().hex}"
        assert (
            await client.post(
                "/auth/bootstrap",
                json={
                    "email": f"{uid}@example.edu",
                    "display_name": "Signup",
                    "account_type": "greek",
                },
                headers={"X-Debug-Firebase-Uid": uid, "X-Forwarded-For": "203.0.113.7"},
            )
        ).status_code == 201

    uid = f"uid-{uuid.uuid4().hex}"
    other_ip = await client.post(
        "/auth/bootstrap",
        json={
            "email": f"{uid}@example.edu",
            "display_name": "Different Dorm",
            "account_type": "greek",
        },
        headers={"X-Debug-Firebase-Uid": uid, "X-Forwarded-For": "203.0.113.8"},
    )
    assert other_ip.status_code == 201, other_ip.text


# ---------------------------------------------------------------------------
# 3. invites — the unauthorized-access vector
# ---------------------------------------------------------------------------


async def test_invite_minting_stops_a_loop_but_not_an_officer(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Heavy real pattern: an officer mints one code per role (c105 codes are
    unlimited-use, so not one per pledge) — a handful per sitting, call it 10."""
    _assert_headroom(10, INVITE_MINT_LIMIT, "invite_mint")
    max_calls, _ = INVITE_MINT_LIMIT
    setup = await make_chapter_with("president")

    for i in range(max_calls):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert response.status_code == 201, f"invite {i + 1}: {response.text}"

    refused = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "invite_mint_rate_limited"


async def test_invite_redemption_limit_also_bounds_code_guessing(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """c105 makes an invite code an unlimited-use bearer token with no revocation, so
    unlimited GUESSES eventually join a chapter uninvited. The limiter runs as a route
    dependency, so a wrong code costs an attempt — which is what makes this a guessing
    control and not just a spam control.

    Real pattern: a student joins one to three chapters ever and retypes a mistaken
    code a few times.
    """
    _assert_headroom(6, INVITE_REDEEM_LIMIT, "invite_redeem")
    max_calls, _ = INVITE_REDEEM_LIMIT
    guesser = await make_user("Guesser")

    for i in range(max_calls):
        response = await client.post(
            "/chapters/join",
            json={"code": uuid.uuid4().hex[:8].upper()},
            headers=guesser.headers,
        )
        assert response.status_code != 429, f"guess {i + 1} refused early: {response.text}"

    refused = await client.post(
        "/chapters/join",
        json={"code": uuid.uuid4().hex[:8].upper()},
        headers=guesser.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "invite_redeem_rate_limited"


# ---------------------------------------------------------------------------
# 4. content writes — spam and flood
# ---------------------------------------------------------------------------


async def test_post_creation_stops_a_flood_but_not_an_event_night(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Heavy real pattern: five posts in a burst around an event."""
    _assert_headroom(5, POST_CREATE_LIMIT, "post_create")
    max_calls, _ = POST_CREATE_LIMIT
    setup = await make_chapter_with("president")

    for i in range(max_calls):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={"body": f"post {i}"},
            headers=setup.president.headers,
        )
        assert response.status_code == 201, f"post {i + 1}: {response.text}"

    refused = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "one too many"},
        headers=setup.president.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "post_create_rate_limited"


async def test_comment_creation_stops_a_flood_but_not_a_lively_thread(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Heavy real pattern: twenty replies from one person in a fast-moving thread.
    Comments run hotter than posts, which is why the ceiling is double."""
    _assert_headroom(20, COMMENT_CREATE_LIMIT, "comment_create")
    max_calls, _ = COMMENT_CREATE_LIMIT
    setup = await make_chapter_with("president")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "a thread"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    for i in range(max_calls):
        response = await client.post(
            f"/posts/{post_id}/comments",
            json={"body": f"reply {i}"},
            headers=setup.president.headers,
        )
        assert response.status_code == 201, f"comment {i + 1}: {response.text}"

    refused = await client.post(
        f"/posts/{post_id}/comments",
        json={"body": "one too many"},
        headers=setup.president.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "comment_create_rate_limited"


async def test_chirp_creation_stops_a_flood_but_not_a_venting_session(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Heavy real pattern: ten chirps in a sitting on the anonymous board."""
    _assert_headroom(10, CHIRP_CREATE_LIMIT, "chirp_create")
    max_calls, _ = CHIRP_CREATE_LIMIT
    campus_id = await make_campus()
    user = await _make_campus_user(client, campus_id)

    for i in range(max_calls):
        response = await client.post(
            f"/campuses/{campus_id}/chirps",
            json={"body": f"chirp {i}"},
            headers=user.headers,
        )
        assert response.status_code == 201, f"chirp {i + 1}: {response.text}"

    refused = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "one too many"},
        headers=user.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "chirp_create_rate_limited"


async def test_message_send_ceiling_is_far_above_human_texting(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """The limit most likely to fire on real use if set carelessly, so it is set far
    above what a person can physically do: 300 in ten minutes is one message every two
    seconds SUSTAINED. A fast back-and-forth is nowhere near it.

    Realistic heavy pattern: 60 messages in a rapid ten-minute exchange.
    """
    _assert_headroom(60, MESSAGE_SEND_LIMIT, "message_send")
    max_calls, _ = MESSAGE_SEND_LIMIT

    sender = await make_user("Sender")
    recipient = await make_user("Recipient")
    await share_verified_campus(sender.id, recipient.id)
    device = await register_device(sender, one_time_prekey_count=1)
    conversation = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [recipient.id]},
        headers=sender.headers,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]

    body = {
        "sender_device_id": device["id"],
        "ciphertext_b64": b64(b"opaque"),
        "message_type": "signal",
    }
    for i in range(max_calls):
        response = await client.post(
            f"/conversations/{conversation_id}/messages",
            json=body,
            headers=sender.headers,
        )
        assert response.status_code == 201, f"message {i + 1}: {response.text}"

    refused = await client.post(
        f"/conversations/{conversation_id}/messages", json=body, headers=sender.headers
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "message_send_rate_limited"


# ---------------------------------------------------------------------------
# 5. reports — moderation abuse
# ---------------------------------------------------------------------------


async def test_report_creation_stops_mass_reporting_but_not_a_real_reporter(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The abuse this bounds is brigading one account with reports to bury them.
    Real pattern: one to five reports in a sitting."""
    _assert_headroom(5, REPORT_CREATE_LIMIT, "report_create")
    max_calls, _ = REPORT_CREATE_LIMIT
    campus_id = await make_campus()
    reporter = await _make_campus_user(client, campus_id, "Reporter")
    author = await _make_campus_user(client, campus_id, "Author")
    chirp = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "report me"},
        headers=author.headers,
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    for i in range(max_calls):
        response = await client.post(
            "/moderation/reports",
            json={"target_type": "chirp", "target_id": chirp_id, "reason": "spam"},
            headers=reporter.headers,
        )
        assert response.status_code == 201, f"report {i + 1}: {response.text}"

    refused = await client.post(
        "/moderation/reports",
        json={"target_type": "chirp", "target_id": chirp_id, "reason": "spam"},
        headers=reporter.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "report_create_rate_limited"


# ---------------------------------------------------------------------------
# cross-cutting properties
# ---------------------------------------------------------------------------


async def test_an_unauthenticated_caller_still_gets_401_not_429(
    client: AsyncClient,
) -> None:
    """Ordering matters. The per-user limiter depends on get_verified_uid, so an
    anonymous caller is refused by authentication first. A 429 here would leak that
    the endpoint exists and is reachable, and would let an anonymous caller burn a
    budget keyed on nothing."""
    response = await client.post(
        "/media/upload-url", json={"content_type": "image/jpeg", "byte_size": 1000}
    )
    assert response.status_code == 401, response.text


def test_every_limit_is_documented_as_a_pair() -> None:
    """Each constant is (max_calls, window_seconds) with both values sane — a typo
    that swapped them would produce a limit of 600 calls per 30 seconds and read as
    plausible in a diff."""
    limits = {
        "media_upload_url": MEDIA_UPLOAD_URL_LIMIT,
        "account_bootstrap": ACCOUNT_BOOTSTRAP_LIMIT,
        "invite_mint": INVITE_MINT_LIMIT,
        "invite_redeem": INVITE_REDEEM_LIMIT,
        "post_create": POST_CREATE_LIMIT,
        "comment_create": COMMENT_CREATE_LIMIT,
        "chirp_create": CHIRP_CREATE_LIMIT,
        "message_send": MESSAGE_SEND_LIMIT,
        "report_create": REPORT_CREATE_LIMIT,
    }
    for name, (max_calls, window_seconds) in limits.items():
        assert 0 < max_calls <= 1000, name
        assert 60 <= window_seconds <= 3600, name
