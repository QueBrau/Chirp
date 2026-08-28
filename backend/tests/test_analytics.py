"""Board c227: structured behavioral analytics, and the hard privacy rule around it.

Two families of test here:

  1. app/core/analytics.py itself - the JSON shape on the wire, and that a failure
     inside emit() (a bad prop, a monkeypatched json.dumps) can never surface as a
     500 on the route that called it.
  2. Every instrumented call site - each real action emits exactly its one event,
     with only the coarse props the card asked for, via a real request through
     `client` and a real caplog capture on the "app.analytics" logger.

THE PRIVACY-RULE TEST (test_chirps_router_never_imports_analytics) is the one that
matters most: it source-scans app/routers/chirps.py for the literal string
"analytics" and fails the whole suite if it appears. Nothing else in this file
enforces that chirp creation/voting/reporting stay unreachable from this pipeline -
this is the falsifying test for it.
"""
from __future__ import annotations

import json
import logging
import types

import pytest
from httpx import AsyncClient

from app.core import analytics
from tests.conftest import (
    BACKEND_DIR,
    MakeCampus,
    MakeChapterWith,
    MakeUser,
    RegisterDevice,
    b64,
)
from tests.test_campus_verification import DOMAIN, _code_from, _set_domains, sent_codes
from tests.test_payments import (
    _create_dues_cycle,
    _onboard,
    stripe_calls,
    stripe_env,
)
from tests.test_payments import (
    _post_webhook as post_stripe_webhook,
)


def _analytics_events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    """Every JSON line this run logged on "app.analytics", decoded and in order."""
    events: list[dict] = []
    for record in caplog.records:
        if record.name != "app.analytics":
            continue
        payload = json.loads(record.getMessage())
        assert payload.get("analytics") is True
        events.append(payload)
    return events


# ---------------------------------------------------------------------------
# app/core/analytics.py itself
# ---------------------------------------------------------------------------


def test_emit_logs_one_json_line_on_the_app_analytics_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.analytics"):
        analytics.emit("unit_test_event", user_id="u1", count=3, ok=True)

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0] == {
        "analytics": True,
        "event": "unit_test_event",
        "user_id": "u1",
        "count": 3,
        "ok": True,
    }


def test_emit_never_raises_when_json_dumps_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The falsifying test for "NEVER RAISES": break json.dumps at exactly the name
    analytics.py's emit() resolves through, and prove emit() still returns normally
    and logs a warning instead of propagating.

    Scoped to the `json` NAME bound inside app.core.analytics's own module
    namespace, not the real json module object - mutating the real module's
    `.dumps` attribute would also break Starlette's own JSONResponse rendering
    (it calls the same `json.dumps`), which would fail this test's *request* for
    an unrelated reason and prove nothing about emit() specifically.
    """

    def boom(*args: object, **kwargs: object) -> str:
        raise TypeError("not serializable, by construction")

    monkeypatch.setattr(analytics, "json", types.SimpleNamespace(dumps=boom))

    with caplog.at_level(logging.WARNING, logger="app.analytics"):
        analytics.emit("unit_test_event", user_id="u1")  # must not raise

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("analytics emit failed" in r.getMessage() for r in warnings)


def test_emit_serializes_uuid_props_with_default_str(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every id handed to emit() from a router is a live uuid.UUID, not a string -
    json.dumps cannot serialize one on its own, so this is what actually keeps every
    call site below from silently degrading into nothing but warnings."""
    import uuid

    an_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="app.analytics"):
        analytics.emit("unit_test_event", user_id=an_id)

    events = _analytics_events(caplog)
    assert events[0]["user_id"] == str(an_id)


# ---------------------------------------------------------------------------
# THE HARD PRIVACY RULE: chirps.py must never reach this module
# ---------------------------------------------------------------------------


def test_chirps_router_never_imports_analytics() -> None:
    """Source-scan, not a mock/monkeypatch check - this must fail loudly the moment
    ANY line in chirps.py so much as mentions "analytics", whether that's a real
    `from app.core.analytics import emit` call, an aliased import, or a re-exported
    wrapper. Chirp creation, voting, and reporting must be structurally unreachable
    from the analytics pipeline (board c227, Jose's hard privacy rule) - the API
    already withholds chirp authorship on purpose, and this is what keeps that true
    on the telemetry side too.
    """
    chirps_source = (BACKEND_DIR / "app" / "routers" / "chirps.py").read_text()
    assert "analytics" not in chirps_source, (
        "app/routers/chirps.py must never reference analytics in any form - "
        "board c227's hard privacy rule. Found the string 'analytics' in the file."
    )


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------


async def test_signup_emits_user_signed_up(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            "/auth/bootstrap",
            json={
                "email": "analytics-signup@example.edu",
                "display_name": "Analytics Signup",
                "account_type": "greek",
            },
            headers={"X-Debug-Firebase-Uid": "uid-analytics-signup"},
        )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "user_signed_up"
    assert events[0]["user_id"] == user_id
    assert events[0]["account_type"] == "greek"
    # Coarse only - the email address must never ride along.
    assert "email" not in events[0]


async def test_chapter_post_created_emits_coarse_props(
    client: AsyncClient, make_chapter_with: MakeChapterWith, caplog: pytest.LogCaptureFixture
) -> None:
    setup = await make_chapter_with(role="member")
    caplog.clear()  # make_chapter_with's own bootstrap calls already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={"body": "hello analytics"},
            headers=setup.member.headers,
        )
    assert response.status_code == 201, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "post_created"
    assert events[0]["user_id"] == setup.member.id
    assert events[0]["chapter_id"] == setup.chapter_id
    assert events[0]["audience"] == "org"
    assert events[0]["post_type"] == "text"
    # Never the post body.
    assert "body" not in events[0]


async def test_message_sent_emits_coarse_props_never_ciphertext(
    client: AsyncClient,
    make_user: MakeUser,
    register_device: RegisterDevice,
    caplog: pytest.LogCaptureFixture,
) -> None:
    creator = await make_user("Analytics Sender")
    other = await make_user("Analytics Recipient")
    device = await register_device(creator, one_time_prekey_count=1)

    created = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    caplog.clear()  # the two make_user() bootstraps already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            f"/conversations/{conversation_id}/messages",
            json={
                "sender_device_id": device["id"],
                "ciphertext_b64": b64(b"top secret"),
            },
            headers=creator.headers,
        )
    assert response.status_code == 201, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "message_sent"
    assert events[0]["user_id"] == creator.id
    assert events[0]["conversation_id"] == conversation_id
    assert events[0]["message_type"] == "signal"
    assert events[0]["recipient_count"] == 2  # creator + other, both members
    assert "ciphertext" not in json.dumps(events[0])
    assert "ciphertext_b64" not in events[0]


async def test_event_created_emits_coarse_props(
    client: AsyncClient, make_chapter_with: MakeChapterWith, caplog: pytest.LogCaptureFixture
) -> None:
    setup = await make_chapter_with(role="member")
    caplog.clear()  # make_chapter_with's own bootstrap calls already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json={
                "title": "Analytics Mixer",
                "starts_at": "2026-09-27T19:00:00Z",
                "location": "Chapter House",
                "cover_url": "https://picsum.photos/seed/analytics/800/600",
            },
            headers=setup.member.headers,
        )
    assert response.status_code == 201, response.text
    event_id = response.json()["id"]

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "event_created"
    assert events[0]["user_id"] == setup.member.id
    assert events[0]["chapter_id"] == setup.chapter_id
    assert events[0]["event_id"] == event_id
    assert events[0]["visibility"] == "chapter"
    assert "title" not in events[0]


async def test_event_rsvp_emits_coarse_props(
    client: AsyncClient, make_chapter_with: MakeChapterWith, caplog: pytest.LogCaptureFixture
) -> None:
    """Covers both "event RSVP" and "invite responded" from the card - this codebase
    has exactly one code path for answering an event (PUT /events/{id}/rsvps),
    reached the same way whether the caller got there via membership or an invite;
    there is no separate accept/decline-invite endpoint to instrument."""
    setup = await make_chapter_with(role="member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json={
            "title": "RSVP Mixer",
            "starts_at": "2026-09-27T19:00:00Z",
            "location": "Chapter House",
            "cover_url": "https://picsum.photos/seed/analytics-rsvp/800/600",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    caplog.clear()  # setup + create_event above already emitted user_signed_up/event_created

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.put(
            f"/events/{event_id}/rsvps",
            json={"status": "going"},
            headers=setup.member.headers,
        )
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "event_rsvp"
    assert events[0]["user_id"] == setup.member.id
    assert events[0]["chapter_id"] == setup.chapter_id
    assert events[0]["event_id"] == event_id
    assert events[0]["status"] == "going"


async def test_payment_intent_created_emits_coarse_props(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    caplog.clear()  # setup above already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            f"/payments/dues/{cycle_id}/intent",
            json={"rail": "card"},
            headers=setup.member.headers,
        )
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "payment_intent_created"
    assert events[0]["chapter_id"] == setup.chapter_id
    assert events[0]["cycle_id"] == cycle_id
    assert events[0]["user_id"] == setup.member.id
    assert events[0]["rail"] == "card"


async def test_same_rail_retry_does_not_double_emit_intent_created(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """c227 skeptic catch: the retrieve path re-serves an existing intent on every
    same-rail retry/poll (for ACH, across days) - the emit lives in the CREATE
    branch only, so retries never inflate the intents-created count."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        first = await client.post(
            f"/payments/dues/{cycle_id}/intent",
            json={"rail": "card"},
            headers=setup.member.headers,
        )
        retry = await client.post(
            f"/payments/dues/{cycle_id}/intent",
            json={"rail": "card"},
            headers=setup.member.headers,
        )
    assert first.status_code == 200 and retry.status_code == 200
    assert len(stripe_calls["payment_intent"]) == 1, "retry stayed on the retrieve path"

    events = [e for e in _analytics_events(caplog) if e["event"] == "payment_intent_created"]
    assert len(events) == 1, "one real creation, one emit - the retry emitted nothing"


async def test_webhook_without_chirp_metadata_emits_nothing(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """c227 skeptic catch: an intent we did not create carries no chirp_* metadata
    - mirrored guard with _record_dues_payment, skipped instead of emitted as a
    junk all-None row."""
    setup = await make_chapter_with(role="member")
    await _create_dues_cycle(client, setup)
    event = {
        "id": "evt_foreign_intent",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_foreign", "metadata": {}}},
    }
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await post_stripe_webhook(client, event)
    assert response.status_code == 200, response.text
    assert _analytics_events(caplog) == []


def _stripe_webhook_event(
    event_type: str, *, cycle_id: str, user_id: str, rail: str = "card"
) -> dict:
    return {
        "id": f"evt_{event_type}_{cycle_id}_{user_id}",
        "type": event_type,
        "data": {
            "object": {
                "id": f"pi_{cycle_id}_{user_id}",
                "metadata": {
                    "chirp_dues_cycle_id": cycle_id,
                    "chirp_user_id": user_id,
                    "chirp_rail": rail,
                },
            }
        },
    }


async def test_webhook_payment_succeeded_emits_coarse_props(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup)
    event = _stripe_webhook_event(
        "payment_intent.succeeded", cycle_id=cycle_id, user_id=setup.member.id, rail="ach"
    )
    caplog.clear()  # setup above already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await post_stripe_webhook(client, event)
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "payment_succeeded"
    assert events[0]["event_type"] == "payment_intent.succeeded"
    assert events[0]["rail"] == "ach"
    assert events[0]["cycle_id"] == cycle_id
    assert events[0]["user_id"] == setup.member.id


async def test_webhook_payment_failed_emits_coarse_props(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup)
    event = _stripe_webhook_event(
        "payment_intent.payment_failed", cycle_id=cycle_id, user_id=setup.member.id, rail="card"
    )
    caplog.clear()  # setup above already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await post_stripe_webhook(client, event)
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "payment_failed"
    assert events[0]["event_type"] == "payment_intent.payment_failed"
    assert events[0]["rail"] == "card"
    assert events[0]["cycle_id"] == cycle_id
    assert events[0]["user_id"] == setup.member.id


async def test_webhook_replay_does_not_double_emit(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The `else:` gate on the commit's try/except matters precisely here: a
    replayed delivery of an already-processed event must not double-count."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup)
    event = _stripe_webhook_event(
        "payment_intent.succeeded", cycle_id=cycle_id, user_id=setup.member.id
    )
    caplog.clear()  # setup above already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        first = await post_stripe_webhook(client, event)
        second = await post_stripe_webhook(client, event)
    assert first.status_code == 200
    assert second.status_code == 200

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "payment_succeeded"


async def test_campus_verification_started_emits_coarse_props(
    client: AsyncClient,
    make_user: MakeUser,
    make_campus: MakeCampus,
    sent_codes: list[dict],
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await make_user()
    campus_id = await make_campus()
    await _set_domains(campus_id, [DOMAIN])
    caplog.clear()  # make_user's own bootstrap call already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            "/auth/campus-verification",
            json={"edu_email": f"student@{DOMAIN}"},
            headers=user.headers,
        )
    assert response.status_code == 202, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "campus_verification_started"
    assert events[0]["user_id"] == user.id
    assert events[0]["campus_id"] == campus_id
    assert "edu_email" not in events[0]


async def test_campus_verification_redeemed_emits_coarse_props(
    client: AsyncClient,
    make_user: MakeUser,
    make_campus: MakeCampus,
    sent_codes: list[dict],
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await make_user()
    campus_id = await make_campus()
    await _set_domains(campus_id, [DOMAIN])
    sent = await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )
    assert sent.status_code == 202, sent.text
    code = _code_from(sent_codes[0])
    caplog.clear()  # the start call above already emitted campus_verification_started

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            "/auth/campus-verification/redeem",
            json={"code": code},
            headers=user.headers,
        )
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "campus_verification_redeemed"
    assert events[0]["user_id"] == user.id
    assert events[0]["campus_id"] == campus_id


async def test_poll_voted_emits_no_voter_id(
    client: AsyncClient, make_chapter_with: MakeChapterWith, caplog: pytest.LogCaptureFixture
) -> None:
    """Secret ballot (board c162): the analytics event must carry poll_id + the
    poll's scope id and NOTHING that identifies who cast the vote."""
    setup = await make_chapter_with(role="secretary")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/polls",
        json={"question": "Approve the budget?", "options": ["Yes", "No"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    poll = created.json()
    option_id = poll["options"][0]["id"]
    caplog.clear()  # setup + poll creation above already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
            json={"option_id": option_id},
            headers=setup.president.headers,
        )
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "poll_voted"
    assert events[0]["poll_id"] == poll["id"]
    assert events[0]["chapter_id"] == setup.chapter_id
    # THE assertion that matters: no id anywhere in the payload can identify the
    # voter (setup.president.id, in this test).
    assert setup.president.id not in events[0].values()
    assert "user_id" not in events[0]
    assert all("user" not in key for key in events[0])


# ---------------------------------------------------------------------------
# A broken emit must never break the request that triggered it
# ---------------------------------------------------------------------------


async def test_a_raising_emit_does_not_break_the_route(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patches json.dumps at exactly the name analytics.emit() resolves through, so
    every emit() call in this process starts raising, then exercises a real
    instrumented route and proves the response is unaffected. This is the
    falsifying test for "never in a way that can fail the request" - reverting
    emit()'s internal try/except turns this red with a 500.

    Scoped to app.core.analytics's own `json` name (see the docstring on
    test_emit_never_raises_when_json_dumps_fails above) rather than the real json
    module - patching the real module's `.dumps` would also break FastAPI's own
    JSON response rendering and turn this into a false positive for the wrong
    reason.
    """

    def boom(*args: object, **kwargs: object) -> str:
        raise TypeError("json.dumps is broken for this test")

    monkeypatch.setattr(analytics, "json", types.SimpleNamespace(dumps=boom))

    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": "analytics-boom@example.edu",
            "display_name": "Analytics Boom",
            "account_type": "greek",
        },
        headers={"X-Debug-Firebase-Uid": "uid-analytics-boom"},
    )
    assert response.status_code == 201, response.text
