"""Durable provider receipts must still authorize the same account on resume."""

import uuid
from datetime import date, timedelta
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import models
from app.db import get_session_factory
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion
from tests.conftest import ApiUser
from tests.test_c436_fulfillment import ConfirmingProvider, FailOnceProvider, provider_set


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "empty", "subject", "provider", "confirmed", "readback", "scope"])
async def test_invalid_resumed_receipt_blocks_all_later_work(
    client: AsyncClient, make_user: Callable[..., Awaitable[ApiUser]], damage: str,
) -> None:
    owner = await make_user("Resume receipt owner")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201
    request_id = uuid.UUID(created.json()["id"])
    providers = provider_set()
    providers["media_storage"] = FailOnceProvider("media_storage")
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert (await run_deletion(session, request_id, user, providers)).status == "blocked"
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "payment_provider",
        ))).one()
        receipt = dict(step.provider_ref)
        if damage == "missing":
            receipt = None
        elif damage == "empty":
            receipt = {}
        elif damage == "subject":
            receipt["subject"] = str(uuid.uuid4())
        elif damage == "provider":
            receipt["provider"] = "media_storage"
        elif damage in {"confirmed", "readback"}:
            receipt[damage] = "true"
        else:
            receipt["scope"] = []
        step.provider_ref = receipt
        await session.commit()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        resumed_providers = provider_set()
        result = await run_deletion(session, request_id, user, resumed_providers)
        assert result.status == "blocked"
        assert result.failed_step == "payment_provider"
        assert all(not provider.keys for provider in resumed_providers.values())
        await session.refresh(user)
        assert user.firebase_uid == owner.firebase_uid
        request = await session.get(models.AccountDataRequest, request_id)
        assert request.failure_code == "provider_step_incomplete"
        assert request.fulfillment_lease_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["subject", "status", "missing_provider", "missing_tombstone"])
async def test_changed_durable_receipt_blocks_final_cleanup(
    client: AsyncClient, make_user: Callable[..., Awaitable[ApiUser]], damage: str,
) -> None:
    owner = await make_user("Final receipt owner")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201
    request_id = uuid.UUID(created.json()["id"])

    class JournalChangingProvider(ConfirmingProvider):
        async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
            async with get_session_factory()() as other:
                step = (await other.scalars(select(models.AccountFulfillmentStep).where(
                    models.AccountFulfillmentStep.request_id == request_id,
                    models.AccountFulfillmentStep.step_key == (
                        "database_tombstone" if damage == "missing_tombstone" else "payment_provider"
                    ),
                ))).one()
                if damage == "subject":
                    step.provider_ref = {**step.provider_ref, "subject": str(uuid.uuid4())}
                elif damage == "status":
                    step.status = "pending"
                else:
                    await other.delete(step)
                await other.commit()
            return await super().delete_account_data(user=user, idempotency_key=idempotency_key)

    providers = provider_set()
    providers[PROVIDER_STEPS[-1]] = JournalChangingProvider(PROVIDER_STEPS[-1])
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "blocked"
        assert result.failed_step == (None if damage.startswith("missing_") else "payment_provider")
        await session.refresh(user)
        assert user.firebase_uid == owner.firebase_uid
        request = await session.get(models.AccountDataRequest, request_id)
        assert request.completed_at is None
        assert request.fulfillment_lease_token is None


@pytest.mark.parametrize(
    ("targets", "exclude_probe", "allowed"),
    [(32, False, True), (31, True, True), (32, True, False), (33, False, False)],
)
def test_analytics_receipt_inventory_bound_includes_probe(
    targets: int, exclude_probe: bool, allowed: bool,
) -> None:
    from app.services.account_fulfillment import ProviderUnavailable, validate_provider_receipt

    subject = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    request_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    receipt = {
        "provider": "analytics", "subject": str(subject), "confirmed": True,
        "readback": True, "request_id": str(request_id), "snapshot_digest": "c" * 64,
        "scope": [f"run_googleapis_com_stdout_{date(2026, 1, 1) + timedelta(days=n):%Y%m%d}" for n in range(targets)],
        "excluded_scope": ["chirps_analytics_probe_20260908"] if exclude_probe else [],
        "time_travel_hours": 168, "fail_safe_days": 7,
        "retained_with_reason": ["bigquery_time_travel", "bigquery_fail_safe"],
    }
    if allowed:
        assert validate_provider_receipt(
            "analytics", subject, receipt, expected_request_id=request_id,
        ) == receipt["retained_with_reason"]
    else:
        with pytest.raises(ProviderUnavailable):
            validate_provider_receipt("analytics", subject, receipt, expected_request_id=request_id)
