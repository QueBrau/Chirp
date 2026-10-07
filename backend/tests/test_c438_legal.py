"""c438 legal acceptance invariants and focused API behavior."""
import asyncio
import pytest
from sqlalchemy import delete, update
from app import models
from app.db import get_session_factory
from pydantic import ValidationError

from app.schemas.legal import LegalAcceptanceCreate


@pytest.mark.asyncio
async def test_acceptance_is_server_versioned_idempotent_and_stale_rejected(client) -> None:
    headers = {"X-Debug-Firebase-Uid": "c438-legal-user"}
    response = await client.post("/auth/bootstrap", headers=headers, json={
        "email": "c438@example.com", "display_name": "C438", "account_type": "non_greek",
    })
    assert response.status_code == 201
    status = await client.get("/auth/legal-status", headers=headers)
    assert status.status_code == 200
    policies = {row["key"]: row["version"] for row in status.json()["policies"]}
    payload = {
        "terms_version": policies["terms"], "privacy_version": policies["privacy"],
        "age_declaration": 18, "guardian_permission_confirmed": False,
    }
    accepted = await client.post("/auth/legal-acceptance", headers=headers, json=payload)
    assert accepted.status_code == 200 and accepted.json()["required"] is False
    repeated = await client.post("/auth/legal-acceptance", headers=headers, json=payload)
    assert repeated.status_code == 200 and repeated.json()["required"] is False
    concurrent = await asyncio.gather(*(
        client.post("/auth/legal-acceptance", headers=headers, json=payload) for _ in range(4)
    ))
    assert all(response.status_code == 200 for response in concurrent)
    stale = await client.post("/auth/legal-acceptance", headers=headers, json={**payload, "terms_version": "old"})
    assert stale.status_code == 409 and stale.json()["detail"] == "legal_policy_changed"
    assert (await client.post("/auth/legal-acceptance", json=payload)).status_code == 401


@pytest.mark.asyncio
async def test_enforcement_blocks_product_routes_but_legal_status_remains_available(client, monkeypatch) -> None:
    headers = {"X-Debug-Firebase-Uid": "c438-enforced"}
    await client.post("/auth/bootstrap", headers=headers, json={
        "email": "enforced-c438@example.com", "display_name": "Enforced", "account_type": "non_greek",
    })
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "legal_enforcement_enabled", True)
    assert (await client.get("/auth/legal-status", headers=headers)).status_code == 200
    blocked = await client.get("/alumni/profile", headers=headers)
    assert blocked.status_code == 428 and blocked.json()["detail"] == "legal_acceptance_required"
    policies = (await client.get("/legal/policies")).json()["policies"]
    payload = {
        "terms_version": next(row["version"] for row in policies if row["key"] == "terms"),
        "privacy_version": next(row["version"] for row in policies if row["key"] == "privacy"),
        "age_declaration": 18,
    }
    assert (await client.post("/auth/legal-acceptance", headers=headers, json=payload)).status_code == 200
    allowed = await client.get("/alumni/profile", headers=headers)
    assert allowed.status_code != 428


@pytest.mark.asyncio
async def test_acceptance_is_account_scoped_and_policy_change_requires_reacceptance(client) -> None:
    first = {"X-Debug-Firebase-Uid": "c438-first"}
    second = {"X-Debug-Firebase-Uid": "c438-second"}
    for headers, email in ((first, "first-c438@example.com"), (second, "second-c438@example.com")):
        response = await client.post("/auth/bootstrap", headers=headers, json={
            "email": email, "display_name": "C438", "account_type": "non_greek",
        })
        assert response.status_code == 201
    current = (await client.get("/legal/policies")).json()["policies"]
    payload = {
        "terms_version": next(row["version"] for row in current if row["key"] == "terms"),
        "privacy_version": next(row["version"] for row in current if row["key"] == "privacy"),
        "age_declaration": 18,
    }
    assert (await client.post("/auth/legal-acceptance", headers=first, json=payload)).json()["required"] is False
    assert (await client.get("/auth/legal-status", headers=second)).json()["required"] is True
    async with get_session_factory()() as session:
        await session.execute(update(models.LegalPolicy).where(models.LegalPolicy.policy_key == "terms").values(is_current=False))
        session.add(models.LegalPolicy(policy_key="terms", version="2026-11-01", is_current=True))
        await session.commit()
    changed = await client.get("/auth/legal-status", headers=first)
    assert changed.status_code == 200 and changed.json()["required"] is True and changed.json()["material_change"] is True


@pytest.mark.asyncio
async def test_empty_policy_configuration_fails_closed_on_me(client) -> None:
    headers = {"X-Debug-Firebase-Uid": "c438-empty-policy"}
    await client.post("/auth/bootstrap", headers=headers, json={
        "email": "empty-c438@example.com", "display_name": "Empty", "account_type": "non_greek",
    })
    async with get_session_factory()() as session:
        await session.execute(delete(models.LegalPolicy))
        await session.commit()
    response = await client.get("/auth/me", headers=headers)
    assert response.status_code == 503 and response.json()["detail"] == "legal_policy_unavailable"


@pytest.mark.asyncio
async def test_bootstrap_minor_without_guardian_is_422(client) -> None:
    response = await client.post("/auth/bootstrap", headers={"X-Debug-Firebase-Uid": "c438-minor"}, json={
        "email": "minor-c438@example.com", "display_name": "Minor", "account_type": "non_greek",
        "terms_version": "2026-10-06", "privacy_version": "2026-10-06", "age_declaration": 17,
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_bootstrap_rejects_overlong_policy_version_as_422(client) -> None:
    response = await client.post("/auth/bootstrap", headers={"X-Debug-Firebase-Uid": "c438-long"}, json={
        "email": "long-c438@example.com", "display_name": "Long", "account_type": "non_greek",
        "terms_version": "x" * 41, "privacy_version": "2026-10-06", "age_declaration": 18,
    })
    assert response.status_code == 422


def test_seventeen_requires_explicit_guardian_confirmation() -> None:
    with pytest.raises(ValidationError, match="guardian_permission_required"):
        LegalAcceptanceCreate(terms_version="2026-10-06", privacy_version="2026-10-06", age_declaration=17)


def test_eighteen_does_not_require_guardian_confirmation() -> None:
    value = LegalAcceptanceCreate(terms_version="2026-10-06", privacy_version="2026-10-06", age_declaration=18)
    assert value.guardian_permission_confirmed is False


def test_seventeen_with_confirmation_is_valid() -> None:
    value = LegalAcceptanceCreate(terms_version="2026-10-06", privacy_version="2026-10-06", age_declaration=17, guardian_permission_confirmed=True)
    assert value.age_declaration == 17


def test_policy_version_length_is_validated_at_request_boundary() -> None:
    with pytest.raises(ValidationError):
        LegalAcceptanceCreate(terms_version="x" * 41, privacy_version="2026-10-06", age_declaration=18)


def test_migration_0040_downgrade_and_upgrade(migrated_db) -> None:
    """The reserved revision can roll back without leaving policy tables behind."""
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.downgrade(config, "0039")
    command.upgrade(config, "0040")
