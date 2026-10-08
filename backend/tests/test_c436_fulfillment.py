"""Real-DB deletion journal tests; providers are explicit synthetic fakes."""

import uuid
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import update

from tests.conftest import ApiUser
from app import models
from app.db import get_session_factory
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion
from app.services.account_cleanup import cleanup_local_account


class ConfirmingProvider:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.keys.append(idempotency_key)
        return {"confirmed": True}


class StrictFalseProvider(ConfirmingProvider):
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]:
        self.keys.append(idempotency_key)
        return {"confirmed": "false"}


class FailOnceProvider(ConfirmingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.keys.append(idempotency_key)
        if not self.failed:
            self.failed = True
            raise RuntimeError("secret provider response must not persist")
        return {"confirmed": True}


class GatedProvider(ConfirmingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        self.keys.append(idempotency_key)
        self.started.set()
        await self.release.wait()
        return {"confirmed": True}


@pytest.mark.asyncio
async def test_deletion_journal_blocks_without_adapters_then_retries_idempotently(
    client: AsyncClient, make_user
) -> None:
    owner: ApiUser = await make_user("Journal owner")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "blocked"
    assert set(body["provider_steps"]) == set((*PROVIDER_STEPS, "database_tombstone"))

    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, uuid.UUID(body["id"]))
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        assert user.firebase_uid == owner.firebase_uid

        blocked = await run_deletion(session, request.id, user, {})
        assert blocked.status == "blocked" and blocked.failed_step == PROVIDER_STEPS[0]
        await session.refresh(user)
        assert user.firebase_uid == owner.firebase_uid

        providers = {key: ConfirmingProvider() for key in PROVIDER_STEPS}
        completed = await run_deletion(session, request.id, user, providers)
        assert completed.status == "completed"
        assert set(completed.completed_steps) == set((*PROVIDER_STEPS, "database_tombstone"))
        await session.refresh(user)
        assert user.firebase_uid == f"deleted:{owner.id}"
        assert user.email.endswith("@invalid.chirp")
        steps = (await session.scalars(
            select(models.AccountFulfillmentStep).where(
                models.AccountFulfillmentStep.request_id == request.id
            )
        )).all()
        assert len(steps) == 5
        assert all(row.status == "succeeded" for row in steps)
        assert all(provider.keys == [f"account-deletion:{request.id}:{key}"] for key, provider in providers.items())

        repeat = await run_deletion(session, request.id, user, {key: StrictFalseProvider() for key in PROVIDER_STEPS})
        assert repeat.status == "completed"


@pytest.mark.asyncio
async def test_missing_adapters_are_preflighted_without_provider_calls(client: AsyncClient, make_user) -> None:
    owner: ApiUser = await make_user("Preflight owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, uuid.UUID(response.json()["id"]))
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        providers = {key: ConfirmingProvider() for key in PROVIDER_STEPS[:-1]}
        result = await run_deletion(session, request.id, user, providers)
        assert result.status == "blocked"
        assert all(not provider.keys for provider in providers.values())


@pytest.mark.asyncio
async def test_failed_prefix_retries_without_replaying_confirmed_steps(client: AsyncClient, make_user) -> None:
    owner: ApiUser = await make_user("Retry owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, uuid.UUID(response.json()["id"]))
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        flaky = {key: ConfirmingProvider() for key in PROVIDER_STEPS}
        flaky[PROVIDER_STEPS[1]] = FailOnceProvider()
        first = await run_deletion(session, request.id, user, flaky)
        assert first.status == "blocked"
        second = await run_deletion(session, request.id, user, flaky)
        assert second.status == "completed"
        assert len(flaky[PROVIDER_STEPS[0]].keys) == 1
        assert "secret provider response" not in (await session.get(models.AccountDataRequest, request.id)).provider_steps


@pytest.mark.asyncio
async def test_canceled_request_is_terminal_and_makes_no_provider_calls(client: AsyncClient, make_user) -> None:
    owner: ApiUser = await make_user("Canceled owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, uuid.UUID(response.json()["id"]))
        user = await session.get(models.User, uuid.UUID(owner.id))
        request.status = "canceled"
        await session.commit()
        providers = {key: ConfirmingProvider() for key in PROVIDER_STEPS}
        result = await run_deletion(session, request.id, user, providers)
        assert result.status == "blocked"
        assert all(not provider.keys for provider in providers.values())


@pytest.mark.asyncio
async def test_local_cleanup_redacts_owner_and_preserves_foreign_rows(client: AsyncClient, make_user, make_campus) -> None:
    owner: ApiUser = await make_user("Cleanup owner")
    other: ApiUser = await make_user("Cleanup other")
    async with get_session_factory()() as session:
        campus = uuid.UUID(await make_campus())
        owner_id, other_id = uuid.UUID(owner.id), uuid.UUID(other.id)
        owned_post = models.Post(campus_id=campus, author_id=owner_id, body="owner body", audience="campus", post_type="text")
        foreign_post = models.Post(campus_id=campus, author_id=other_id, body="foreign body", audience="campus", post_type="text")
        session.add_all([owned_post, foreign_post])
        await session.flush()
        session.add_all([
            models.PostComment(post_id=owned_post.id, author_id=owner_id, body="comment"),
            models.Chirp(campus_id=campus, author_id=owner_id, body="chirp"),
            models.JobPost(posted_by=owner_id, title="job", company="company", description="description"),
        ])
        await session.commit()
        result = await cleanup_local_account(session, owner_id)
        await session.commit()
        await session.refresh(owned_post)
        await session.refresh(foreign_post)
        assert owned_post.body == "[deleted]" and owned_post.deleted_at is not None
        assert foreign_post.body == "foreign body" and foreign_post.deleted_at is None
        assert result["retained"]["financial_history"]


@pytest.mark.asyncio
async def test_two_sessions_have_one_provider_owner(client: AsyncClient, make_user) -> None:
    owner: ApiUser = await make_user("Race owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    gate = GatedProvider()
    providers = {key: ConfirmingProvider() for key in PROVIDER_STEPS}
    providers[PROVIDER_STEPS[0]] = gate
    async with get_session_factory()() as first, get_session_factory()() as second:
        first_user = await first.get(models.User, uuid.UUID(owner.id))
        second_user = await second.get(models.User, uuid.UUID(owner.id))
        assert first_user is not None and second_user is not None
        first_task = asyncio.create_task(run_deletion(first, request_id, first_user, providers))
        await asyncio.wait_for(gate.started.wait(), timeout=5)
        second_task = asyncio.create_task(run_deletion(second, request_id, second_user, providers))
        gate.release.set()
        first_result = await asyncio.wait_for(first_task, timeout=5)
        second_result = await asyncio.wait_for(second_task, timeout=5)
    assert second_result.status == "blocked"
    assert first_result.status == "completed"
    assert len(gate.keys) == 1


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_overwritten_by_stale_owner(client: AsyncClient, make_user) -> None:
    owner: ApiUser = await make_user("Lease owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == PROVIDER_STEPS[0],
        ))).one()
        old_token = uuid.uuid4()
        step.status = "running"
        step.lease_token = old_token
        step.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
        new_token = uuid.uuid4()
        claimed = await session.execute(update(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.id == step.id,
            models.AccountFulfillmentStep.status == "running",
            models.AccountFulfillmentStep.lease_expires_at < datetime.now(timezone.utc),
        ).values(lease_token=new_token, lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
        assert claimed.rowcount == 1
        stale = await session.execute(update(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.id == step.id,
            models.AccountFulfillmentStep.status == "running",
            models.AccountFulfillmentStep.lease_token == old_token,
        ).values(status="succeeded"))
        assert stale.rowcount == 0
