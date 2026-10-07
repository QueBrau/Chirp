"""c438 legal acceptance invariants that do not require a provider or database."""
import pytest
from app import models
from app.db import get_session_factory
from pydantic import ValidationError

from app.schemas.legal import LegalAcceptanceCreate


@pytest.mark.asyncio
async def test_acceptance_is_server_versioned_idempotent_and_stale_rejected(client) -> None:
    async with get_session_factory()() as session:
        session.add_all([
            models.LegalPolicy(policy_key="terms", version="2026-10-06"),
            models.LegalPolicy(policy_key="privacy", version="2026-10-06"),
        ])
        await session.commit()
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
    stale = await client.post("/auth/legal-acceptance", headers=headers, json={**payload, "terms_version": "old"})
    assert stale.status_code == 409 and stale.json()["detail"] == "legal_policy_changed"


@pytest.mark.asyncio
async def test_bootstrap_minor_without_guardian_is_422(client) -> None:
    response = await client.post("/auth/bootstrap", headers={"X-Debug-Firebase-Uid": "c438-minor"}, json={
        "email": "minor-c438@example.com", "display_name": "Minor", "account_type": "non_greek",
        "terms_version": "2026-10-06", "privacy_version": "2026-10-06", "age_declaration": 17,
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


def test_migration_0040_downgrade_and_upgrade(migrated_db) -> None:
    """The reserved revision can roll back without leaving policy tables behind."""
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.downgrade(config, "0039")
    command.upgrade(config, "0040")
