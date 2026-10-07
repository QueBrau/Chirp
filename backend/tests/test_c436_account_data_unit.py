"""Focused c436 safety tests that do not contact a provider or mutate a database."""

import uuid

import pytest

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
