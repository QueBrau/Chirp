"""Focused tests for the explicit, default-off deferred-cleanup operator job."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db import get_session_factory
from app.jobs.account_deferred_cleanup import (
    CleanupBinding,
    CleanupOperatorError,
    build_media_reader,
    inspect_one,
    main,
)


@pytest.mark.asyncio
async def test_inspect_fails_closed_before_claim_on_unprepared_schema() -> None:
    async with get_session_factory()() as session:
        identity = (await session.execute(text(
            "SELECT current_database(), current_user"
        ))).one()
        binding = CleanupBinding(
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "temporary_media", "0" * 64,
        )
        with pytest.raises(CleanupOperatorError, match="schema_head_mismatch"):
            await inspect_one(
                session, binding,
                expected_role=str(identity[1]), expected_database=str(identity[0]),
            )


def test_apply_requires_explicit_provider_factory_and_does_not_connect(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://invalid.invalid/nope")
    result = main([
        "--obligation-id", str(uuid.uuid4()), "--request-id", str(uuid.uuid4()),
        "--expected-user-id", str(uuid.uuid4()), "--category", "temporary_media",
        "--scope-sha256", "0" * 64, "--expected-role", "chirp_api",
        "--expected-database", "chirp", "--apply-one",
    ])
    assert result == 2
    assert capsys.readouterr().out == '{"status": "blocked", "error": "cleanup_preflight_failed"}\n'


def test_media_factory_requires_explicit_project_and_selection() -> None:
    with pytest.raises(CleanupOperatorError, match="media_project_invalid"):
        build_media_reader(bucket="chirp-media", project="wrong", use_adc=True)
    with pytest.raises(CleanupOperatorError, match="media_credentials_selection_invalid"):
        build_media_reader(bucket="chirp-media", project="chirps-prod")
