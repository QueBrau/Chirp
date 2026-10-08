"""Real-DB deletion journal tests; providers are explicit synthetic fakes."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.conftest import ApiUser
from app import models
from app.db import get_session_factory
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion


class ConfirmingProvider:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]:
        self.keys.append(idempotency_key)
        return {"confirmed": "true"}


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
