"""Firebase worker cancellation stays inside the deletion lease boundary."""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app import models
from app.db import get_session_factory
from app.services import account_fulfillment as fulfillment
from app.services.account_fulfillment import FirebaseProvider, PROVIDER_STEPS, ProviderUnavailable, run_deletion
from tests.conftest import ApiUser
from tests.test_c436_fulfillment import ConfirmingProvider


def firebase_user() -> object:
    return type("FirebaseUser", (), {"id": uuid.uuid4(), "firebase_uid": "firebase-c457"})()


async def release_worker(task: asyncio.Task[object], release: threading.Event) -> None:
    release.set()
    if task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
    except BaseException:
        pass
    assert task.done(), "test worker did not drain within the cleanup bound"


@pytest.mark.asyncio
async def test_timeout_drains_blocking_delete_before_return(monkeypatch) -> None:
    from firebase_admin import auth

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    readback = threading.Event()

    def delete_user(uid: str) -> None:
        assert uid == "firebase-c457"
        started.set()
        if not release.wait(10):
            raise AssertionError("test release was not delivered")
        finished.set()

    def get_user(uid: str) -> object:
        readback.set()
        raise AssertionError("unexpected Firebase readback")

    monkeypatch.setattr(auth, "delete_user", delete_user)
    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(fulfillment, "FIREBASE_CALL_TIMEOUT_SECONDS", 0.05)
    task = asyncio.create_task(FirebaseProvider().delete_account_data(
        user=firebase_user(), idempotency_key="firebase-c457-timeout"
    ))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.08)
        assert not task.done(), "timeout must drain the SDK worker before returning"
        release.set()
        with pytest.raises(ProviderUnavailable, match="^firebase_delete_failed$"):
            await task
    finally:
        await release_worker(task, release)
    assert finished.is_set()
    assert not readback.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_worker_and_propagates_cancel(monkeypatch) -> None:
    from firebase_admin import auth

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    readback = threading.Event()

    def delete_user(uid: str) -> None:
        started.set()
        if not release.wait(10):
            raise AssertionError("test release was not delivered")
        finished.set()

    def get_user(uid: str) -> object:
        readback.set()
        raise AssertionError("unexpected Firebase readback")

    monkeypatch.setattr(auth, "delete_user", delete_user)
    monkeypatch.setattr(auth, "get_user", get_user)
    task = asyncio.create_task(FirebaseProvider().delete_account_data(
        user=firebase_user(), idempotency_key="firebase-c457-cancel"
    ))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await release_worker(task, release)
    assert finished.is_set()
    assert not readback.is_set()


@pytest.mark.asyncio
async def test_late_sdk_exception_is_sanitized_after_timeout(monkeypatch) -> None:
    from firebase_admin import auth

    started = threading.Event()
    release = threading.Event()
    readback = threading.Event()

    def delete_user(uid: str) -> None:
        started.set()
        if not release.wait(10):
            raise AssertionError("test release was not delivered")
        raise RuntimeError("private Firebase response body")

    def get_user(uid: str) -> object:
        readback.set()
        raise AssertionError("unexpected Firebase readback")

    monkeypatch.setattr(auth, "delete_user", delete_user)
    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(fulfillment, "FIREBASE_CALL_TIMEOUT_SECONDS", 0.05)
    task = asyncio.create_task(FirebaseProvider().delete_account_data(
        user=firebase_user(), idempotency_key="firebase-c457-error"
    ))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.08)
        release.set()
        with pytest.raises(ProviderUnavailable) as caught:
            await task
    finally:
        await release_worker(task, release)
    assert str(caught.value) == "firebase_delete_failed"
    assert "private Firebase" not in str(caught.value)
    assert not readback.is_set()


@pytest.mark.asyncio
async def test_real_deletion_lease_remains_until_firebase_worker_drains(
    client: AsyncClient, make_user, monkeypatch
) -> None:
    from firebase_admin import auth

    owner: ApiUser = await make_user("Firebase worker lease")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    request_id = uuid.UUID(response.json()["id"])
    started = threading.Event()
    release = threading.Event()
    readback = threading.Event()

    def delete_user(uid: str) -> None:
        started.set()
        if not release.wait(10):
            raise AssertionError("test release was not delivered")

    def get_user(uid: str) -> object:
        readback.set()
        raise AssertionError("unexpected Firebase readback")

    monkeypatch.setattr(auth, "delete_user", delete_user)
    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(fulfillment, "FIREBASE_CALL_TIMEOUT_SECONDS", 0.05)
    providers = {key: ConfirmingProvider(key) for key in PROVIDER_STEPS}
    providers["firebase_auth"] = FirebaseProvider()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        task = asyncio.create_task(run_deletion(session, request_id, user, providers))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            async with get_session_factory()() as observer:
                request = await observer.get(models.AccountDataRequest, request_id)
                step = (await observer.scalars(select(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == "firebase_auth",
                ))).one()
                assert request is not None
                assert request.fulfillment_lease_token is not None
                assert step.status == "running"
                assert step.lease_token is not None
            await asyncio.sleep(0.08)
            assert not task.done()
            async with get_session_factory()() as observer:
                request = await observer.get(models.AccountDataRequest, request_id)
                step = (await observer.scalars(select(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == "firebase_auth",
                ))).one()
                assert request is not None
                assert request.fulfillment_lease_token is not None
                assert step.status == "running"
                assert step.lease_token is not None
            release.set()
            result = await task
            assert result.status == "blocked"
        finally:
            await release_worker(task, release)
    async with get_session_factory()() as observer:
        request = await observer.get(models.AccountDataRequest, request_id)
        step = (await observer.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "firebase_auth",
        ))).one()
        assert request is not None
        assert request.status == "blocked"
        assert request.fulfillment_lease_token is None
        assert step.status == "manual_review"
        assert step.lease_token is None
    assert not readback.is_set()


@pytest.mark.asyncio
async def test_expired_firebase_attempt_is_manual_review_and_never_reinvoked(
    client: AsyncClient, make_user, monkeypatch
) -> None:
    from firebase_admin import auth

    owner: ApiUser = await make_user("Firebase uncertain outcome")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    request_id = uuid.UUID(response.json()["id"])
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def delete_user(uid: str) -> None:
        calls.append(uid)
        started.set()
        if not release.wait(10):
            raise AssertionError("test release was not delivered")

    def get_user(uid: str) -> object:
        raise AssertionError("unexpected Firebase readback")

    monkeypatch.setattr(auth, "delete_user", delete_user)
    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(fulfillment, "FIREBASE_CALL_TIMEOUT_SECONDS", 0.05)
    providers = {key: ConfirmingProvider(key) for key in PROVIDER_STEPS}
    providers["firebase_auth"] = FirebaseProvider()
    first_task: asyncio.Task[object] | None = None
    try:
        async with get_session_factory()() as first:
            user = await first.get(models.User, uuid.UUID(owner.id))
            assert user is not None
            first_task = asyncio.create_task(run_deletion(first, request_id, user, providers))
            assert await asyncio.to_thread(started.wait, 5)
            async with get_session_factory()() as expire:
                await expire.execute(update(models.AccountDataRequest).where(
                    models.AccountDataRequest.id == request_id,
                ).values(fulfillment_lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                await expire.execute(update(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == "firebase_auth",
                ).values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                await expire.commit()
            await asyncio.sleep(0.08)
            assert not first_task.done()
            async with get_session_factory()() as second:
                second_user = await second.get(models.User, uuid.UUID(owner.id))
                assert second_user is not None
                second_result = await run_deletion(second, request_id, second_user, providers)
                assert second_result.status == "blocked"
            async with get_session_factory()() as third:
                third_user = await third.get(models.User, uuid.UUID(owner.id))
                assert third_user is not None
                third_result = await run_deletion(third, request_id, third_user, providers)
                assert third_result.status == "blocked"
            assert calls == [owner.firebase_uid]
            async with get_session_factory()() as inspect_session:
                request = await inspect_session.get(models.AccountDataRequest, request_id)
                step = (await inspect_session.scalars(select(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == "firebase_auth",
                ))).one()
                assert request is not None
                assert request.status == "blocked"
                assert request.fulfillment_lease_token is None
                assert step.status == "manual_review"
                assert step.last_error == "firebase_outcome_unknown"
                assert step.lease_token is None
            release.set()
            result = await first_task
            assert result.status == "blocked"
            async with get_session_factory()() as final_session:
                step = (await final_session.scalars(select(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == "firebase_auth",
                ))).one()
                assert step.status == "manual_review"
                assert step.last_error == "firebase_outcome_unknown"
    finally:
        release.set()
        if first_task is not None and not first_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(first_task), timeout=3)
            except BaseException:
                pass
        if first_task is not None:
            assert first_task.done()
