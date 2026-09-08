"""Fixed-pool local provider delays expose retained DB slots and blocking auth."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import firebase_admin
import pytest
import stripe
from firebase_admin import auth as firebase_auth
from sqlalchemy import event

from app.config import get_settings
from app.db import get_engine
from app.middleware import auth
from app.routers import feed
from app.services import storage_service
from tests.test_payments import _create_dues_cycle, _onboard, stripe_calls, stripe_env


@pytest.fixture(autouse=True)
def fixed_pool_budget(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "1")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def transaction_probe(monkeypatch):
    engine = get_engine()
    assert engine.pool.size() == 1 and engine.pool._max_overflow == 0
    held_since = {}
    durations, transactions, acquisitions = [], [], []
    transactions_started = {}
    def begin(connection):
        transactions_started[id(connection)] = time.monotonic()
    def end(connection):
        started = transactions_started.pop(id(connection), None)
        if started is not None:
            transactions.append(time.monotonic() - started)
    original_get = engine.pool._do_get
    def acquire():
        before = time.monotonic()
        try:
            return original_get()
        finally:
            acquisitions.append(time.monotonic() - before)
    monkeypatch.setattr(engine.pool, "_do_get", acquire)
    event.listen(engine.sync_engine, "begin", begin)
    event.listen(engine.sync_engine, "commit", end)
    event.listen(engine.sync_engine, "rollback", end)
    def checkout(connection, record, proxy):
        held_since[id(record)] = time.monotonic()
    def checkin(connection, record):
        started = held_since.pop(id(record), None)
        if started is not None:
            durations.append(time.monotonic() - started)
    event.listen(engine.sync_engine, "checkout", checkout)
    event.listen(engine.sync_engine, "checkin", checkin)
    return engine, durations, transactions, acquisitions


async def test_slow_storage_finalize_releases_the_only_db_slot(
    client, make_chapter_with, monkeypatch,
):
    setup = await make_chapter_with("member")
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "local-fixture-media")
    entered, release = threading.Event(), threading.Event()
    def finalize(user_id, name, **kwargs):
        entered.set()
        assert release.wait(5), "local provider fixture was never released"
        return f"https://storage.googleapis.com/local-fixture-media/posts/{user_id}/probe.jpg"
    monkeypatch.setattr(feed, "finalize_media_object", finalize)
    engine, durations, transactions, acquisitions = transaction_probe(monkeypatch)
    request = asyncio.create_task(client.post(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers,
        json={"body": "pool probe", "post_type": "photo",
              "media_object_names": [f"tmp/{setup.member.id}/probe.jpg"]},
    ))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        checked_out = engine.pool.checkedout()
        started = time.monotonic()
        unrelated = await client.get("/auth/me", headers=setup.member.headers)
        wait = time.monotonic() - started
    finally:
        release.set()
        response = await request
    assert response.status_code == 201, response.text
    evidence = {"scope": "storage_finalize", "pool": 1, "checked_out_during_provider": checked_out,
                "unrelated_status": unrelated.status_code, "unrelated_wait_ms": round(wait*1000, 2),
                "max_checkout_ms": round(max(durations)*1000, 2),
                "max_transaction_ms": round(max(transactions)*1000, 2),
                "max_pool_acquisition_ms": round(max(acquisitions)*1000, 2)}
    print(json.dumps(evidence))
    assert checked_out == 0 and unrelated.status_code == 200, evidence


@pytest.mark.parametrize("step", ["account", "customer"])
async def test_stripe_preparation_releases_the_only_db_slot(
    client, make_chapter_with, monkeypatch, stripe_env, stripe_calls, step,
):
    setup = await make_chapter_with("member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    target, method = (stripe.Account, "retrieve_async") if step == "account" else (stripe.Customer, "create_async")
    original = getattr(target, method)
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(target, method, delayed)
    engine, durations, transactions, acquisitions = transaction_probe(monkeypatch)
    request = asyncio.create_task(client.post(
        f"/payments/dues/{cycle}/intent", json={"rail": "card"}, headers=setup.member.headers,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        checked_out = engine.pool.checkedout()
        started = time.monotonic()
        unrelated = await client.get("/auth/me", headers=setup.member.headers)
        wait = time.monotonic() - started
    finally:
        release.set()
        response = await request
    assert response.status_code == 200, response.text
    evidence = {"scope": f"stripe_{step}", "pool": 1, "checked_out_during_provider": checked_out,
                "unrelated_status": unrelated.status_code, "unrelated_wait_ms": round(wait*1000, 2),
                "max_checkout_ms": round(max(durations)*1000, 2),
                "max_transaction_ms": round(max(transactions)*1000, 2),
                "max_pool_acquisition_ms": round(max(acquisitions)*1000, 2)}
    print(json.dumps(evidence))
    assert checked_out == 0 and unrelated.status_code == 200, evidence


async def test_slow_firebase_verification_does_not_stall_the_event_loop(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(auth_mode="firebase"))
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())
    started, finished = threading.Event(), threading.Event()
    verifier_threads, tick_times, lag = [], [], []
    def verify(token):
        verifier_threads.append(threading.get_ident())
        started.set()
        time.sleep(0.25)  # local certificate-fetch delay; no SDK/network activity
        finished.set()
        return {"uid": "fixture-uid", "email": "fixture@example.edu"}
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    async def heartbeat():
        while not finished.is_set():
            before = time.monotonic()
            await asyncio.sleep(0.005)
            lag.append(time.monotonic() - before - 0.005)
            if started.is_set() and not finished.is_set():
                tick_times.append(time.monotonic())
    ticker = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    result = await auth.get_verified_identity(None, "Bearer local-fixture-token")
    await ticker
    assert result == ("fixture-uid", "fixture@example.edu")
    evidence = {"scope": "firebase", "provider_delay_ms": 250,
                "event_loop_max_lag_ms": round(max(lag)*1000, 2),
                "ticks_during_verification": len(tick_times),
                "verification_on_loop": verifier_threads == [threading.get_ident()]}
    print(json.dumps(evidence))
    assert verifier_threads != [threading.get_ident()] and tick_times, evidence


async def test_canceled_verification_keeps_admission_until_the_worker_finishes(monkeypatch):
    from app.services import identity_verification
    from app.services.identity_verification import VERIFICATION_WORKERS

    submitted = []
    original_submit = identity_verification._executor.submit
    def submit(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        submitted.append(future)
        return future
    monkeypatch.setattr(identity_verification._executor, "submit", submit)

    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(auth_mode="firebase"))
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())
    release = threading.Event()
    late_release = threading.Event()
    lock = threading.Lock()
    starts, active, peak = [], 0, 0
    def verify(token):
        nonlocal active, peak
        with lock:
            starts.append(token)
            active += 1
            peak = max(peak, active)
        try:
            assert release.wait(5), "worker fixture was not released"
            if token == "fixture-0":
                # A newly admitted call can finish before a canceled caller's
                # native worker. Force the scheduling order that CI exposed.
                assert late_release.wait(5), "late worker fixture was not released"
            return {"uid": "fixture-uid"}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    requests = [asyncio.create_task(auth.get_verified_uid(None, f"Bearer fixture-{i}"))
                for i in range(VERIFICATION_WORKERS)]
    queued = None
    try:
        async with asyncio.timeout(3):
            while len(starts) < VERIFICATION_WORKERS:
                await asyncio.sleep(0.005)
        queued = asyncio.create_task(auth.get_verified_uid(None, "Bearer next-fixture"))
        for request in requests:
            request.cancel()  # native asyncio cancellation, not only an AnyIO scope
        results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), 0.5)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        await asyncio.sleep(0.05)
        assert len(starts) == VERIFICATION_WORKERS and not queued.done()
        assert len(submitted) == VERIFICATION_WORKERS, "canceled callers must not refill the executor queue"
        waiting = asyncio.create_task(auth.get_verified_uid(None, "Bearer canceled-waiter"))
        await asyncio.sleep(0)
        waiting.cancel()
        assert isinstance((await asyncio.gather(waiting, return_exceptions=True))[0], asyncio.CancelledError)
        assert len(submitted) == VERIFICATION_WORKERS
    finally:
        release.set()
        try:
            await asyncio.gather(*requests, return_exceptions=True)
            if queued is not None:
                assert await asyncio.wait_for(queued, 3) == "fixture-uid"
                with lock:
                    assert active >= 1, "the late native worker must still be held"
        finally:
            late_release.set()
            # Canceled asyncio callers are already done; joining them does not
            # join the native futures that still own executor admission.
            await asyncio.wait_for(asyncio.gather(*(
                asyncio.wrap_future(future) for future in submitted
            )), 3)
    assert peak == VERIFICATION_WORKERS and active == 0
    assert len(submitted) == VERIFICATION_WORKERS + 1


async def mutate(sql, params=None):
    from sqlalchemy import text
    from app.db import get_session_factory
    async with get_session_factory()() as session:
        await session.execute(text(sql), params or {})
        await session.commit()


@pytest.mark.parametrize("route,change,expected", [
    ("chapter", "removed", "not_a_member"),
    ("chapter", "suspended", "account_suspended"),
    ("chapter_campus", "unverified", "campus_unverified"),
    ("campus", "unverified", "campus_unverified"),
    ("edit", "removed", "not_a_member"),
    ("edit", "deleted", "post_not_found"),
    ("edit_campus", "unverified", "campus_unverified"),
])
async def test_media_write_revalidates_after_provider_wait(
    client, make_chapter_with, monkeypatch, route, change, expected,
):
    from tests.conftest import verify_campus
    from tests.test_c349_settlement_binding import rows

    setup = await make_chapter_with("member")
    await verify_campus(setup.member.id)
    campus_id = str((await rows("SELECT campus_id FROM chapters WHERE id=:id", {"id": setup.chapter_id}))[0]["campus_id"])
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "local-fixture-media")
    endpoint = f"/chapters/{setup.chapter_id}/posts"
    method, post_id = "POST", None
    if route in ("edit", "edit_campus"):
        original = await client.post(endpoint, json={"body": "original", "audience": "campus" if route == "edit_campus" else "org"}, headers=setup.member.headers)
        assert original.status_code == 201, original.text
        post_id = original.json()["id"]
        endpoint += f"/{post_id}"
        method = "PATCH"
    elif route == "campus":
        endpoint = f"/campuses/{campus_id}/posts"
    entered, release = threading.Event(), threading.Event()
    def finalize(user_id, name, **kwargs):
        entered.set()
        assert release.wait(5)
        return f"https://storage.googleapis.com/local-fixture-media/posts/{user_id}/staged.jpg"
    monkeypatch.setattr(feed, "finalize_media_object", finalize)
    body = {"body": "changed", "media_object_names": [f"tmp/{setup.member.id}/staged.jpg"]}
    if route == "chapter_campus":
        body["audience"] = "campus"
    request = asyncio.create_task(client.request(method, endpoint, json=body, headers=setup.member.headers))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert get_engine().pool.checkedout() == 0
        if post_id:
            assert (await rows("SELECT body FROM posts WHERE id=:id", {"id": post_id}))[0]["body"] == "original"
        if change == "removed":
            await mutate("UPDATE memberships SET status='removed' WHERE user_id=:id", {"id": setup.member.id})
        elif change == "suspended":
            await mutate("UPDATE users SET suspended_at=now() WHERE id=:id", {"id": setup.member.id})
        elif change == "unverified":
            await mutate("UPDATE users SET campus_verified_at=NULL WHERE id=:id", {"id": setup.member.id})
        else:
            await mutate("UPDATE posts SET deleted_at=now() WHERE id=:id", {"id": post_id})
    finally:
        release.set()
        response = await request
    assert response.status_code == (404 if change == "deleted" else 403), response.text
    assert response.json()["detail"] == expected
    stored = await rows("SELECT body, media_urls FROM posts")
    assert stored == ([{"body": "original", "media_urls": None}] if post_id else [])


@pytest.mark.parametrize("change,expected", [
    ("removed", "not_a_member"), ("suspended", "account_suspended"),
    ("plan", "on_payment_plan"), ("paid", "already_paid"),
    ("account", "chapter_not_onboarded"), ("amount", None),
])
async def test_dues_revalidates_eligibility_and_routing_after_customer_wait(
    client, make_chapter_with, monkeypatch, stripe_env, stripe_calls, change, expected,
):
    from tests.test_c349_settlement_binding import rows
    from tests.test_dues_payment_plans import _create_plan, _three_installments
    from tests.test_payments import _pay_on_ledger

    setup = await make_chapter_with("member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    entered, release = asyncio.Event(), asyncio.Event()
    original = stripe.Customer.create_async
    async def delayed(**params):
        entered.set()
        await release.wait()
        return await original(**params)
    monkeypatch.setattr(stripe.Customer, "create_async", delayed)
    request = asyncio.create_task(client.post(f"/payments/dues/{cycle}/intent", json={"rail": "card"},
                                               headers=setup.member.headers))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert get_engine().pool.checkedout() == 0
        if change == "removed":
            await mutate("UPDATE memberships SET status='removed' WHERE user_id=:id", {"id": setup.member.id})
        elif change == "suspended":
            await mutate("UPDATE users SET suspended_at=now() WHERE id=:id", {"id": setup.member.id})
        elif change == "plan":
            plan = await _create_plan(client, setup, cycle, setup.member.id, _three_installments(25_000))
            assert plan.status_code == 201, plan.text
        elif change == "paid":
            await _pay_on_ledger(client, setup, cycle, setup.member.id)
        elif change == "account":
            await mutate("UPDATE chapters SET stripe_account_id='acct_rotated' WHERE id=:id", {"id": setup.chapter_id})
        else:
            await mutate("UPDATE dues_cycles SET amount_cents=17000 WHERE id=:id", {"id": cycle})
    finally:
        release.set()
        response = await request
    if change == "amount":
        assert response.status_code == 200, response.text
        assert stripe_calls["payment_intent"][0]["amount"] == 17_000
        assert await rows("SELECT amount_cents FROM dues_payment_intents") == [{"amount_cents": 17_000}]
    else:
        assert response.status_code == (403 if change in ("removed", "suspended") else 409), response.text
        assert response.json()["detail"] == expected
        assert stripe_calls["payment_intent"] == []
        assert await rows("SELECT user_id FROM chapter_stripe_customers") == []
        assert await rows("SELECT id FROM dues_payment_intents") == []


@pytest.mark.parametrize("route,suspended", [(r, s) for r in ("avatar", "upload") for s in (False, True)])
async def test_avatar_and_upload_signing_release_and_revalidate(
    client, make_user, monkeypatch, route, suspended,
):
    from app.routers import auth as auth_router, media
    from tests.test_c349_settlement_binding import rows

    user = await make_user("Original name")
    initial = (await rows("SELECT display_name, avatar_url, account_type FROM users WHERE id=:id", {"id": user.id}))[0]
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "local-fixture-media")
    entered, release = threading.Event(), threading.Event()
    def provider(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        if route == "avatar":
            return f"https://storage.googleapis.com/local-fixture-media/avatars/{user.id}/profile.jpg"
        return SimpleNamespace(upload_url="https://fixture.test/signed", preview_url="https://fixture.test/preview",
                               object_name=f"tmp/{user.id}/profile.jpg", expires_in_seconds=300)
    if route == "avatar":
        monkeypatch.setattr(auth_router, "finalize_media_object", provider)
        request = asyncio.create_task(client.patch("/auth/me", headers=user.headers, json={
            "display_name": "Updated name", "account_type": "alumni",
            "avatar_object_name": f"tmp/{user.id}/profile.jpg",
        }))
    else:
        monkeypatch.setattr(media, "generate_upload_url", provider)
        request = asyncio.create_task(client.post("/media/upload-url", headers=user.headers,
                                                   json={"content_type": "image/jpeg", "byte_size": 1000}))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert get_engine().pool.checkedout() == 0
        assert (await client.get("/auth/me", headers=user.headers)).status_code == 200
        # Profile changes cannot commit as a side effect of releasing the pool.
        assert (await rows("SELECT display_name, avatar_url, account_type FROM users WHERE id=:id", {"id": user.id}))[0] == initial
        if suspended:
            await mutate("UPDATE users SET suspended_at=now() WHERE id=:id", {"id": user.id})
    finally:
        release.set()
        response = await request
    if suspended:
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "account_suspended"
        assert (await rows("SELECT display_name, avatar_url, account_type FROM users WHERE id=:id", {"id": user.id}))[0] == initial
    else:
        assert response.status_code == (200 if route == "avatar" else 201), response.text
        if route == "avatar":
            assert response.json()["display_name"] == "Updated name"
            assert response.json()["account_type"] == "alumni"


@pytest.mark.parametrize("step,revoked", [(p, r) for p in ("onboard_create", "onboard_link", "status") for r in (False, True)])
async def test_onboarding_and_status_release_and_revalidate(
    client, make_chapter_with, monkeypatch, stripe_env, stripe_calls, step, revoked,
):
    from tests.test_c349_settlement_binding import rows

    setup = await make_chapter_with("member")
    if step != "onboard_create":
        await _onboard(client, setup)
    target, method = {
        "onboard_create": (stripe.Account, "create_async"),
        "onboard_link": (stripe.AccountLink, "create_async"),
        "status": (stripe.Account, "retrieve_async"),
    }[step]
    original = getattr(target, method)
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **params):
        entered.set()
        await release.wait()
        return await original(*args, **params)
    monkeypatch.setattr(target, method, delayed)
    request = asyncio.create_task(
        client.get(f"/chapters/{setup.chapter_id}/payments/status", headers=setup.president.headers)
        if step == "status" else client.post("/payments/connect/onboarding-link",
                                             headers=setup.president.headers, json={"chapter_id": setup.chapter_id})
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert get_engine().pool.checkedout() == 0
        assert (await client.get("/auth/me", headers=setup.member.headers)).status_code == 200
        if revoked:
            if step == "status":
                await mutate("UPDATE memberships SET status='removed' WHERE user_id=:id", {"id": setup.president.id})
            else:
                await mutate("UPDATE memberships SET role='member' WHERE user_id=:id", {"id": setup.president.id})
    finally:
        release.set()
        response = await request
    if revoked:
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == ("not_a_member" if step == "status" else "insufficient_role")
        if step == "onboard_create":
            assert await rows("SELECT stripe_account_id FROM chapters") == [{"stripe_account_id": None}]
            assert stripe_calls["account_link"] == []
    else:
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["onboarding", "customer"])
async def test_competing_provider_preparation_keeps_the_database_winner(
    client, make_chapter_with, monkeypatch, stripe_env, stripe_calls, kind,
):
    from tests.test_c349_settlement_binding import rows
    from tests.test_payments import FakeStripeObject

    setup = await make_chapter_with("member")
    if kind == "customer":
        await _onboard(client, setup)
        cycle = await _create_dues_cycle(client, setup)
        target = stripe.Customer
        endpoint, body, headers = f"/payments/dues/{cycle}/intent", {"rail": "card"}, setup.member.headers
    else:
        target = stripe.Account
        endpoint, body, headers = "/payments/connect/onboarding-link", {"chapter_id": setup.chapter_id}, setup.president.headers
    gates = [asyncio.Event(), asyncio.Event()]
    entered = []
    async def create(**params):
        index = len(entered)
        entered.append(params)
        await gates[index].wait()
        return FakeStripeObject(id=f"{'cus' if kind == 'customer' else 'acct'}_candidate_{index}")
    monkeypatch.setattr(target, "create_async", create)
    first = asyncio.create_task(client.post(endpoint, json=body, headers=headers))
    second = None
    try:
        async with asyncio.timeout(5):
            while len(entered) < 1:
                await asyncio.sleep(0.005)
            second = asyncio.create_task(client.post(endpoint, json=body, headers=headers))
            while len(entered) < 2:
                await asyncio.sleep(0.005)
        assert get_engine().pool.checkedout() == 0
        gates[1].set()
        winner = await asyncio.wait_for(second, 5)
        assert winner.status_code == 200, winner.text
    finally:
        for gate in gates:
            gate.set()
        first_response = await first
        if second is not None:
            await second
    assert first_response.status_code == 200, first_response.text
    if kind == "onboarding":
        assert await rows("SELECT stripe_account_id FROM chapters") == [{"stripe_account_id": "acct_candidate_1"}]
        assert [call["account"] for call in stripe_calls["account_link"]] == ["acct_candidate_1", "acct_candidate_1"]
    else:
        assert await rows("SELECT stripe_customer_id FROM chapter_stripe_customers") == [{"stripe_customer_id": "cus_candidate_1"}]
        assert len(stripe_calls["payment_intent"]) == 1
        assert stripe_calls["payment_intent"][0]["customer"] == "cus_candidate_1"
        assert [call["customer"] for call in stripe_calls["customer_session"]] == ["cus_candidate_1", "cus_candidate_1"]
        assert first_response.json()["payment_intent_client_secret"] == winner.json()["payment_intent_client_secret"]


async def test_wrong_provider_account_cannot_authorize_payment_preparation(
    client, make_chapter_with, monkeypatch, stripe_env, stripe_calls,
):
    from tests.test_payments import FakeStripeObject
    setup = await make_chapter_with("member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    async def wrong_account(*args, **params):
        return FakeStripeObject(id="acct_other", charges_enabled=True, details_submitted=True)
    monkeypatch.setattr(stripe.Account, "retrieve_async", wrong_account)
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "card"}, headers=setup.member.headers)
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "payment_outcome_unconfirmed"
    assert stripe_calls["customer_create"] == stripe_calls["payment_intent"] == []
