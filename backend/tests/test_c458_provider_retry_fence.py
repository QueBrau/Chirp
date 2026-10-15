"""Two-runner retry fencing for account-deletion provider calls."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app import models
from app.db import get_session_factory
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion
from app.jobs.account_deletion_preflight import inspect_request


class ConfirmingProvider:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls = 0

    def _receipt(self, user: models.User, idempotency_key: str) -> dict[str, object]:
        receipt: dict[str, object] = {
            "confirmed": True, "provider": self.key, "subject": str(user.id),
            "scope": [self.key], "readback": True,
        }
        if self.key == "analytics":
            receipt.update({
                "time_travel_hours": 168, "fail_safe_days": 7,
                "excluded_scope": [], "request_id": idempotency_key.split(":")[1],
                "snapshot_digest": "0" * 64,
                "scope": ["run_googleapis_com_stdout_20261012"],
                "retained_with_reason": ["bigquery_time_travel", "bigquery_fail_safe"],
            })
        return receipt

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.calls += 1
        return self._receipt(user, idempotency_key)


class GatedProvider(ConfirmingProvider):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release = asyncio.Event()

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
        elif self.calls == 2:
            self.second_started.set()
        await self.release.wait()
        return self._receipt(user, idempotency_key)


def _providers(gated: GatedProvider) -> dict[str, ConfirmingProvider]:
    providers = {key: ConfirmingProvider(key) for key in PROVIDER_STEPS}
    providers[gated.key] = gated
    return providers


class RaisesAfterEffect(ConfirmingProvider):
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.calls += 1
        raise RuntimeError("provider mutation succeeded but response was lost")


class InvalidReceiptProvider(ConfirmingProvider):
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.calls += 1
        return {
            "confirmed": True, "provider": self.key, "subject": "wrong-subject",
            "scope": [self.key], "readback": True,
        }


def _standard_providers() -> dict[str, ConfirmingProvider]:
    return {key: ConfirmingProvider(key) for key in PROVIDER_STEPS}


@pytest.mark.asyncio
@pytest.mark.parametrize("step_key", [
    "payment_provider", "media_storage", "email_logs_backups", "analytics",
])
async def test_expired_running_provider_step_is_quarantined_across_runners(
    client, make_user, step_key: str,
) -> None:
    owner = await make_user(f"Retry fence {step_key}")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])
    gate = GatedProvider(step_key)
    providers = _providers(gate)

    first_task = second_task = third_task = second_waiter = None
    first_session = second_session = third_session = None
    gate.release.clear()
    try:
        first_session = get_session_factory()()
        async with first_session:
            first_user = await first_session.get(models.User, uuid.UUID(owner.id))
            assert first_user is not None
            first_task = asyncio.create_task(
                run_deletion(first_session, request_id, first_user, providers)
            )
            await asyncio.wait_for(gate.first_started.wait(), timeout=5)

            # Emulate a worker that still owns a provider call after both persisted
            # leases expire. A second independent transaction may now try to resume.
            async with get_session_factory()() as expirer:
                await expirer.execute(update(models.AccountDataRequest).where(
                    models.AccountDataRequest.id == request_id,
                ).values(
                    fulfillment_lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                ))
                await expirer.execute(update(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == step_key,
                ).values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                await expirer.commit()

            second_session = get_session_factory()()
            async with second_session:
                second_user = await second_session.get(models.User, uuid.UUID(owner.id))
                assert second_user is not None
                second_task = asyncio.create_task(
                    run_deletion(second_session, request_id, second_user, providers)
                )
                second_waiter = asyncio.create_task(gate.second_started.wait())
                done, _pending = await asyncio.wait(
                    [second_task, second_waiter], timeout=5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                second_invoked = second_waiter in done and second_waiter.result()
                if not second_waiter.done():
                    second_waiter.cancel()
                await asyncio.gather(second_waiter, return_exceptions=True)

                # A third runner must also stay out while the second is active or
                # after it quarantines an unknown outcome.
                third_session = get_session_factory()()
                async with third_session:
                    third_user = await third_session.get(models.User, uuid.UUID(owner.id))
                    assert third_user is not None
                    third_task = asyncio.create_task(
                        run_deletion(third_session, request_id, third_user, providers)
                    )
                    third_result = await asyncio.wait_for(third_task, timeout=5)

                gate.release.set()
                first_result, second_result = await asyncio.wait_for(
                    asyncio.gather(first_task, second_task), timeout=10,
                )

                async with get_session_factory()() as inspect:
                    request = await inspect.get(models.AccountDataRequest, request_id)
                    step = (await inspect.scalars(select(models.AccountFulfillmentStep).where(
                        models.AccountFulfillmentStep.request_id == request_id,
                        models.AccountFulfillmentStep.step_key == step_key,
                    ))).one()
                    user = await inspect.get(models.User, uuid.UUID(owner.id))
                    assert request is not None and user is not None
                    outcome = (
                        request.status, request.failure_code,
                        request.provider_steps[step_key], step.status, step.last_error,
                        step.attempt_count, step.lease_token, step.lease_expires_at,
                        user.firebase_uid, user.is_ghost,
                    )

        # Baseline currently invokes the provider twice; the repair must preserve one
        # invocation, mark the ambiguous step unknown, and never allow local tombstone.
        assert second_invoked is False
        assert gate.calls == 1
        assert first_result.status == second_result.status == third_result.status == "blocked"
        assert outcome == (
            "blocked", "provider_step_incomplete", "manual_review", "manual_review",
            "provider_outcome_unknown", 1, None, None, owner.firebase_uid, False,
        )
        async with get_session_factory()() as session:
            request = await session.get(models.AccountDataRequest, request_id)
            assert request is not None
            report = await inspect_request(
                session, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
            )
        assert report["unknown_outcome_steps"] == [step_key]
        assert "provider_outcome_unknown" in report["blockers"]
        assert report["durable_completion_evidence"] is False
    finally:
        gate.release.set()
        tasks = [task for task in (first_task, second_task, third_task, second_waiter) if task is not None]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("step_key", [
    "payment_provider", "media_storage", "email_logs_backups", "analytics",
])
async def test_canceled_provider_call_stays_running_then_retry_quarantines_without_replay(
    client, make_user, step_key: str,
) -> None:
    owner = await make_user(f"Canceled retry fence {step_key}")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])
    gate = GatedProvider(step_key)
    providers = _providers(gate)
    first_task = None
    try:
        async with get_session_factory()() as first:
            user = await first.get(models.User, uuid.UUID(owner.id))
            assert user is not None
            first_task = asyncio.create_task(run_deletion(first, request_id, user, providers))
            await asyncio.wait_for(gate.first_started.wait(), timeout=5)
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first_task, timeout=5)

        # Cancellation leaves the committed invocation marked running. Once its
        # leases expire, a fresh runner must quarantine that unknown outcome.
        async with get_session_factory()() as expire:
            request = await expire.get(models.AccountDataRequest, request_id)
            step = (await expire.scalars(select(models.AccountFulfillmentStep).where(
                models.AccountFulfillmentStep.request_id == request_id,
                models.AccountFulfillmentStep.step_key == step_key,
            ))).one()
            assert request is not None
            assert request.status == "processing"
            assert step.status == "running" and step.attempt_count == 1
            request.fulfillment_lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            step.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await expire.commit()

        async with get_session_factory()() as retry:
            user = await retry.get(models.User, uuid.UUID(owner.id))
            assert user is not None
            result = await run_deletion(retry, request_id, user, providers)
        assert result.status == "blocked"
        assert gate.calls == 1
        async with get_session_factory()() as inspect:
            request = await inspect.get(models.AccountDataRequest, request_id)
            step = (await inspect.scalars(select(models.AccountFulfillmentStep).where(
                models.AccountFulfillmentStep.request_id == request_id,
                models.AccountFulfillmentStep.step_key == step_key,
            ))).one()
            user = await inspect.get(models.User, uuid.UUID(owner.id))
            assert request is not None and user is not None
            assert request.status == "blocked"
            assert step.status == "manual_review"
            assert step.last_error == "provider_outcome_unknown"
            assert request.provider_steps[step_key] == "manual_review"
            assert user.firebase_uid == owner.firebase_uid and user.is_ghost is False
            report = await inspect_request(
                inspect, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
            )
        assert report["unknown_outcome_steps"] == [step_key]
        assert "provider_outcome_unknown" in report["blockers"]
        assert report["durable_completion_evidence"] is False
    finally:
        gate.release.set()
        if first_task is not None:
            if not first_task.done():
                first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_exception_is_quarantined_and_valid_prefix_receipt_is_reused(client, make_user):
    owner = await make_user("Provider exception quarantine")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    providers = _standard_providers()
    providers["media_storage"] = RaisesAfterEffect("media_storage")

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        first = await run_deletion(session, request_id, user, providers)
        second = await run_deletion(session, request_id, user, providers)
    assert first.status == second.status == "blocked"
    assert providers["payment_provider"].calls == 1
    assert providers["media_storage"].calls == 1
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "media_storage",
        ))).one()
        report = await inspect_request(
            session, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
        )
    assert step.status == "manual_review"
    assert step.last_error == "provider_outcome_unknown"
    assert report["unknown_outcome_steps"] == ["media_storage"]
    assert "provider_outcome_unknown" in report["blockers"]
    assert report["durable_completion_evidence"] is False


@pytest.mark.asyncio
async def test_invalid_provider_receipt_is_quarantined_and_not_reinvoked(client, make_user):
    owner = await make_user("Invalid provider receipt quarantine")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    providers = _standard_providers()
    providers["payment_provider"] = InvalidReceiptProvider("payment_provider")

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        first = await run_deletion(session, request_id, user, providers)
        second = await run_deletion(session, request_id, user, providers)
    assert first.status == second.status == "blocked"
    assert providers["payment_provider"].calls == 1
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "payment_provider",
        ))).one()
    assert step.status == "manual_review"
    assert step.last_error == "provider_outcome_unknown"
    assert step.provider_ref is None


@pytest.mark.asyncio
async def test_missing_provider_pending_plan_stays_resumable(client, make_user):
    owner = await make_user("Pending provider plan")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        missing = await run_deletion(session, request_id, user, {})
        pending = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key.in_(PROVIDER_STEPS),
        ))).all()
        assert missing.status == "blocked"
        assert all(step.status == "pending" and step.attempt_count == 0 for step in pending)

        providers = _standard_providers()
        resumed = await run_deletion(session, request_id, user, providers)
    assert resumed.status == "completed"
    assert all(provider.calls == 1 for provider in providers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["failed", "manual_review"])
async def test_legacy_attempted_nonterminal_step_is_quarantined(client, make_user, legacy_status: str):
    owner = await make_user(f"Legacy retry status {legacy_status}")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "email_logs_backups",
        ))).one()
        step.status = legacy_status
        step.last_error = "provider_error"
        step.attempt_count = 1
        await session.commit()

    providers = _standard_providers()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        result = await run_deletion(session, request_id, user, providers)
    assert result.status == "blocked"
    assert providers["email_logs_backups"].calls == 0
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "email_logs_backups",
        ))).one()
    assert step.status == "manual_review"
    assert step.last_error == "provider_outcome_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,step_key,status,last_error,fields",
    [
        ("expired_running", "payment_provider", "running", None,
         {"lease_token": uuid.uuid4(), "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=2)}),
        ("pending_started", "media_storage", "pending", None,
         {"started_at": datetime.now(timezone.utc) - timedelta(minutes=1)}),
        ("pending_lease_token", "email_logs_backups", "pending", None,
         {"lease_token": uuid.uuid4(), "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=2)}),
        ("generic_unknown_marker", "analytics", "pending", "provider_outcome_unknown", {}),
        ("firebase_unknown_marker", "firebase_auth", "pending", "firebase_outcome_unknown", {}),
        ("failed_zero_attempts", "payment_provider", "failed", "provider_error", {}),
        ("completion_history", "media_storage", "pending", None,
         {"completed_at": datetime.now(timezone.utc) - timedelta(minutes=1)}),
        ("receipt_history", "email_logs_backups", "pending", None,
         {"provider_ref": {"confirmed": True, "provider": "email_logs_backups"}}),
        ("succeeded_bad_receipt_zero_attempts", "analytics", "succeeded", None,
         {"completed_at": datetime.now(timezone.utc) - timedelta(minutes=1),
          "provider_ref": {"confirmed": True, "provider": "analytics", "subject": "wrong-user"}}),
        ("negative_attempt_count", "payment_provider", "pending", None,
         {"attempt_count": -1}),
    ],
)
async def test_count_zero_or_malformed_legacy_state_is_never_replayed(
    client, make_user, case: str, step_key: str, status: str,
    last_error: str | None, fields: dict[str, object],
) -> None:
    owner = await make_user(f"Ambiguous legacy {case}")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == step_key,
        ))).one()
        step.status = status
        step.attempt_count = 0
        step.last_error = last_error
        for name, value in fields.items():
            setattr(step, name, value)
        await session.commit()

    # The operator view must surface the same ambiguity the executor uses.
    async with get_session_factory()() as session:
        report = await inspect_request(
            session, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
        )
    assert step_key in report["unknown_outcome_steps"]
    assert "provider_outcome_unknown" in report["blockers"]
    assert report["durable_completion_evidence"] is False

    providers = _standard_providers()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        first = await run_deletion(session, request_id, user, providers)
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        second = await run_deletion(session, request_id, user, providers)

    assert first.status == second.status == "blocked"
    assert providers[step_key].calls == 0
    assert providers["firebase_auth"].calls == 0
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == step_key,
        ))).one()
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        assert request.status == "blocked"
        assert user.is_ghost is False and user.firebase_uid == owner.firebase_uid
        assert step.status == "manual_review"
        assert step.last_error == (
            "firebase_outcome_unknown" if step_key == "firebase_auth"
            else "provider_outcome_unknown"
        )
        final_report = await inspect_request(
            session, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
        )
    assert step_key in final_report["unknown_outcome_steps"]
    assert final_report["durable_completion_evidence"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["unknown_marker", "residual_lease"])
async def test_valid_succeeded_receipt_with_unknown_fence_cannot_authorize_next_step(
    client, make_user, fence: str,
) -> None:
    owner = await make_user(f"Succeeded receipt fence {fence}")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "payment_provider",
        ))).one()
        assert user is not None
        step.status = "succeeded"
        step.attempt_count = 1
        step.completed_at = datetime.now(timezone.utc)
        step.idempotency_key = f"account-deletion:{request_id}:payment_provider"
        step.provider_ref = ConfirmingProvider("payment_provider")._receipt(
            user, step.idempotency_key,
        )
        if fence == "unknown_marker":
            step.last_error = "provider_outcome_unknown"
        else:
            step.lease_token = uuid.uuid4()
            step.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    providers = _standard_providers()
    results = []
    for _ in range(2):
        async with get_session_factory()() as session:
            user = await session.get(models.User, uuid.UUID(owner.id))
            assert user is not None
            results.append(await run_deletion(session, request_id, user, providers))
    assert [result.status for result in results] == ["blocked", "blocked"]
    assert all(provider.calls == 0 for provider in providers.values())
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "payment_provider",
        ))).one()
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        assert request.status == "blocked"
        assert step.status == "manual_review" and step.last_error == "provider_outcome_unknown"
        assert user.is_ghost is False and user.firebase_uid == owner.firebase_uid
        report = await inspect_request(
            session, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
        )
    assert report["unknown_outcome_steps"] == ["payment_provider"]
    assert report["durable_completion_evidence"] is False


@pytest.mark.asyncio
async def test_completed_request_with_later_unknown_marker_stays_historical_and_blocked(client, make_user):
    owner = await make_user("Terminal request unknown marker")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    providers = _standard_providers()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        completed = await run_deletion(session, request_id, user, providers)
    assert completed.status == "completed"
    assert all(provider.calls == 1 for provider in providers.values())

    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "payment_provider",
        ))).one()
        assert request is not None
        historical_completed_at = request.completed_at
        step.last_error = "provider_outcome_unknown"
        await session.commit()

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        retry = await run_deletion(session, request_id, user, providers)
    assert retry.status == "blocked"
    assert all(provider.calls == 1 for provider in providers.values())
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        assert request.status == "completed" and request.completed_at == historical_completed_at
        assert user.is_ghost is True and user.firebase_uid == f"deleted:{user.id}"
        report = await inspect_request(
            session, request_id=request_id, expected_user_id=uuid.UUID(owner.id),
        )
    assert report["unknown_outcome_steps"] == ["payment_provider"]
    assert report["durable_completion_evidence"] is False
