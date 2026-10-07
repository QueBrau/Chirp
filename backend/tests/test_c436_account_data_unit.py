"""Focused c436 safety tests that do not contact a provider or mutate a database."""

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import ApiUser

from app import models
from app.services import account_data


@pytest.mark.asyncio
async def test_unconfigured_provider_fails_closed() -> None:
    provider = account_data.UnconfiguredProvider("firebase_auth")
    assert await provider.fulfill_deletion(firebase_uid="uid", email="person@example.test") is False


def test_unknown_account_edge_does_not_silently_disappear() -> None:
    with pytest.raises(RuntimeError, match="unclassified_account_data_edge"):
        # The check happens before any session query, so this synthetic session is
        # enough to prove a schema mapping typo cannot become an empty export.
        import asyncio
        asyncio.run(account_data._rows(object(), models.User, "not_a_user_column", uuid.uuid4()))


@pytest.mark.asyncio
async def test_blocked_deletion_does_not_tombstone_account(monkeypatch: pytest.MonkeyPatch) -> None:
    user = models.User(
        id=uuid.uuid4(), firebase_uid="live-uid", email="person@example.test",
        display_name="Person", account_type="non_greek", pseudonym_seed="seed",
    )
    request = models.AccountDataRequest(user_id=user.id, kind="deletion", open_key="open")
    called = False

    async def should_not_run(_session, _user):
        nonlocal called
        called = True

    monkeypatch.setattr(account_data, "_delete_owned_rows", should_not_run)
    result = await account_data.fulfill_request(object(), request, user)
    assert result.status == "blocked"
    assert result.failure_code == "provider_fulfillment_required"
    assert called is False
    assert user.firebase_uid == "live-uid"
    assert user.email == "person@example.test"


@pytest.mark.asyncio
async def test_provider_success_allows_core_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    user = models.User(
        id=uuid.uuid4(), firebase_uid="live-uid", email="person@example.test",
        display_name="Person", account_type="non_greek", pseudonym_seed="seed",
    )
    request = models.AccountDataRequest(user_id=user.id, kind="deletion", open_key="open")
    deleted = False

    class FakeProvider:
        name = "synthetic_provider"

        async def fulfill_deletion(self, *, firebase_uid: str, email: str) -> bool:
            assert firebase_uid == "live-uid"
            assert email == "person@example.test"
            return True

    async def delete_core(_session, _user):
        nonlocal deleted
        deleted = True

    monkeypatch.setattr(account_data, "PROVIDER_ADAPTERS", (FakeProvider(),))
    monkeypatch.setattr(account_data, "_delete_owned_rows", delete_core)
    result = await account_data.fulfill_request(object(), request, user)
    assert result.status == "completed"
    assert deleted is True


@pytest.mark.asyncio
async def test_request_status_is_owner_scoped_and_provider_block_is_retryable(
    client: AsyncClient, make_user
) -> None:
    owner: ApiUser = await make_user("Owner")
    other: ApiUser = await make_user("Other")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "blocked"
    request_id = body["id"]

    assert (await client.get(f"/me/data-requests/{request_id}", headers=other.headers)).status_code == 404
    own = await client.get(f"/me/data-requests/{request_id}", headers=owner.headers)
    assert own.status_code == 200
    assert own.json()["failure_code"] == "provider_fulfillment_required"
    retry = await client.post(f"/me/data-requests/{request_id}/retry", headers=owner.headers)
    assert retry.status_code == 200
    assert retry.json()["status"] == "blocked"
    # The failed provider preflight leaves the account available for status/retry.
    assert (await client.get("/auth/me", headers=owner.headers)).status_code == 200


@pytest.mark.asyncio
async def test_export_is_immutable_authenticated_and_explicitly_partial(
    client: AsyncClient, make_user
) -> None:
    owner: ApiUser = await make_user("Export owner")
    created = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "partially_completed"
    assert body["download_url"] is None
    export = await client.get(f"/me/data-requests/{body['id']}/download", headers=owner.headers)
    assert export.status_code == 200
    assert export.headers["content-disposition"].startswith("attachment;")
    assert export.json()["format"] == "chirp-account-export-v1"
