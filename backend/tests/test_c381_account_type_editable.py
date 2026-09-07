"""c381: account_type becomes editable via PATCH /auth/me.

Board bug: a real user created their account as "alumni", Profile never showed the
alumni section — not even in Edit Layout mode, which is what pointed at the cause
(app-mobile/app/(tabs)/profile/index.tsx:531 checks user.account_type == "alumni"
before it ever looks at section.visible). The server was not returning "alumni" for
him. Two things combined: (auth)/account-type.tsx could submit a pre-selection
nobody tapped, and account_type was written exactly once, at bootstrap
(routers/auth.py), with no route that could ever change it afterward. This file
covers the second half: PATCH /auth/me can now change it.

THE SAFETY ARGUMENT THIS FILE LEANS ON (schemas/identity.py's AccountType docstring,
also re-verified by grep for this card): account_type drives presentation only — the
label under the name on Profile and whether the alumni section renders — and used to
also gate POST /jobs, which c242 already closed (routers/alumni.py's create_job_post
reads a membership row, never this field). Making the field editable is safe
specifically BECAUSE nothing gates on it any more; test_new_attack_surface below
re-confirms that against the ONE new thing this card adds — changing the field
post-signup, which c242's own tests never exercised because the field could not be
changed when they were written.

Three families here:
  1. The round trip and its edges (valid, invalid, explicit null, omitted).
  2. Analytics: a change emits its own event (account_type_changed), never a
     replayed user_signed_up, and a same-value "change" emits nothing at all.
  3. The new-attack-surface check: switching to "alumni" post-signup must not grant
     anything c242 already closed off.
"""
from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser
from tests.test_analytics import _analytics_events

# ---------------------------------------------------------------------------
# Round trip and its edges
# ---------------------------------------------------------------------------


async def test_account_type_round_trips_through_profile_update(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user("Braul", account_type="non_greek")

    updated = await client.patch(
        "/auth/me", json={"account_type": "alumni"}, headers=user.headers
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["account_type"] == "alumni"

    # Persisted, not just echoed back by the write (same shape of proof
    # test_profile_picture.py's test_setting_a_picture_survives_a_reread uses).
    me = await client.get("/auth/me", headers=user.headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["account_type"] == "alumni"


async def test_invalid_account_type_is_rejected(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Outside the three literals - ordinary pydantic validation, before the route
    body is ever touched. "student" specifically, since it's the kind of value a
    client author might type by analogy with the app's own friendlier copy
    ("I'm a student") without checking the wire values are non_greek/greek/alumni."""
    user = await make_user("Someone", account_type="non_greek")

    response = await client.patch(
        "/auth/me", json={"account_type": "student"}, headers=user.headers
    )
    assert response.status_code == 422, response.text

    me = await client.get("/auth/me", headers=user.headers)
    assert me.json()["user"]["account_type"] == "non_greek", "the bad request must not partially apply"


async def test_explicit_null_account_type_is_rejected(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Unlike avatar_object_name, there is no "clear it" state to reach here - the
    column is NOT NULL. Same refusal shape as display_name_cannot_be_cleared."""
    user = await make_user("Someone Else", account_type="greek")

    response = await client.patch("/auth/me", json={"account_type": None}, headers=user.headers)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "account_type_cannot_be_cleared"

    me = await client.get("/auth/me", headers=user.headers)
    assert me.json()["user"]["account_type"] == "greek"


async def test_omitting_account_type_leaves_it_unchanged(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Omitted vs explicit null/value, the model_fields_set contract ProfileUpdate's
    docstring describes - a display-name-only edit must not disturb account_type,
    the same way test_profile_picture.py proves it must not disturb the avatar."""
    user = await make_user("Name Changer", account_type="alumni")

    renamed = await client.patch(
        "/auth/me", json={"display_name": "New Name"}, headers=user.headers
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "New Name"
    assert renamed.json()["account_type"] == "alumni", "a name-only edit changed account_type"


async def test_the_route_only_ever_edits_the_caller_account_type(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """No user id in the path or body (same invariant test_profile_picture.py pins
    for display_name) - one caller's account_type change must never reach another's
    row."""
    one = await make_user("One", account_type="non_greek")
    two = await make_user("Two", account_type="non_greek")

    await client.patch("/auth/me", json={"account_type": "alumni"}, headers=one.headers)

    other = await client.get("/auth/me", headers=two.headers)
    assert other.json()["user"]["account_type"] == "non_greek"


# ---------------------------------------------------------------------------
# Analytics: a change is its own event, never a replayed signup
# ---------------------------------------------------------------------------


async def test_changing_account_type_emits_its_own_event_not_user_signed_up(
    client: AsyncClient, make_user: MakeUser, caplog: pytest.LogCaptureFixture
) -> None:
    user = await make_user("Analytics Change", account_type="non_greek")
    caplog.clear()  # make_user's own bootstrap already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.patch(
            "/auth/me", json={"account_type": "alumni"}, headers=user.headers
        )
    assert response.status_code == 200, response.text

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "account_type_changed"
    assert events[0]["user_id"] == user.id
    assert events[0]["previous_account_type"] == "non_greek"
    assert events[0]["account_type"] == "alumni"
    # THE assertion that matters most: this is a change, not a second signup.
    assert all(e["event"] != "user_signed_up" for e in events)


async def test_setting_the_same_account_type_emits_nothing(
    client: AsyncClient, make_user: MakeUser, caplog: pytest.LogCaptureFixture
) -> None:
    """Not a change at all - re-sending the current value must be a true no-op,
    same shape as c227's same-rail-retry guard for payment intents."""
    user = await make_user("No-Op", account_type="greek")
    caplog.clear()  # make_user's own bootstrap already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.patch(
            "/auth/me", json={"account_type": "greek"}, headers=user.headers
        )
    assert response.status_code == 200, response.text
    assert response.json()["account_type"] == "greek"

    assert _analytics_events(caplog) == []


async def test_changing_account_type_alongside_other_fields_emits_one_event(
    client: AsyncClient, make_user: MakeUser, caplog: pytest.LogCaptureFixture
) -> None:
    """A combined edit (name + account_type in the same body) must not double-fire
    or drop either change."""
    user = await make_user("Combined Edit", account_type="non_greek")
    caplog.clear()  # make_user's own bootstrap already emitted user_signed_up

    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.patch(
            "/auth/me",
            json={"display_name": "Combined Editor", "account_type": "alumni"},
            headers=user.headers,
        )
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Combined Editor"
    assert response.json()["account_type"] == "alumni"

    events = _analytics_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "account_type_changed"


# ---------------------------------------------------------------------------
# The new attack surface this card adds: switching to "alumni" AFTER signup
# ---------------------------------------------------------------------------


async def test_switching_to_alumni_after_signup_still_cannot_post_a_job(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c242 pinned that a self-declared account_type ("alumni" at signup) grants
    nothing on POST /jobs - eligibility comes from a membership role. This card adds
    a path c242's own tests could not exercise, because the field could not change
    then: a plain member switching to "alumni" AFTER signup, mid-session, via this
    new route. Confirms the fix in routers/alumni.py:153 (ELIGIBILITY COMES FROM A
    MEMBERSHIP, NEVER FROM users.account_type) holds against it too.
    """
    setup = await make_chapter_with(role="member")

    switched = await client.patch(
        "/auth/me", json={"account_type": "alumni"}, headers=setup.member.headers
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["account_type"] == "alumni"

    job = {
        "chapter_id": setup.chapter_id,
        "title": "Summer Analyst",
        "company": "Northgate Capital",
        "location": "Chicago, IL",
        "description": "Paid summer internship on the private credit team.",
    }
    response = await client.post("/jobs", json=job, headers=setup.member.headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "alumni_or_eboard_only"
