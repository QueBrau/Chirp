"""Focused tests for the explicit, default-off deferred-cleanup operator job."""
from __future__ import annotations

import uuid
import json
import os

import pytest
from sqlalchemy import text

from app.db import get_session_factory
from app.jobs.account_deferred_cleanup import (
    CleanupBinding,
    CleanupOperatorError,
    build_media_reader,
    inspect_one,
    main,
    run_one_batch,
)
import app.jobs.account_deferred_cleanup as cleanup_job
from app.services.account_post_deletion_cleanup import DeferredCleanupReceipt


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


def test_apply_manifest_is_blocked_by_default_before_provider_construction(tmp_path, monkeypatch, capsys) -> None:
    obligation_id, request_id, user_id = (str(uuid.uuid4()) for _ in range(3))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": "0050", "obligation_id": obligation_id,
        "request_id": request_id, "user_id": user_id,
        "category": "temporary_media", "scope_sha256": "0" * 64,
        "media": {"project": "chirps-prod", "bucket": "chirp-media",
                   "use_adc": True, "versioning_enabled": False,
                   "soft_delete_seconds": 0},
    }))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://invalid.invalid/nope")
    result = cleanup_job.main([
        "--obligation-id", obligation_id, "--request-id", request_id,
        "--expected-user-id", user_id, "--category", "temporary_media",
        "--scope-sha256", "0" * 64, "--expected-role", "chirp_api",
        "--expected-database", "chirp", "--apply-one",
        "--provider-manifest", str(manifest),
    ])
    assert result == 2
    assert capsys.readouterr().out == '{"status": "blocked", "error": "cleanup_preflight_failed"}\n'


def test_media_factory_requires_explicit_project_and_selection() -> None:
    with pytest.raises(CleanupOperatorError, match="media_project_invalid"):
        build_media_reader(bucket="chirp-media", project="wrong", use_adc=True)
    with pytest.raises(CleanupOperatorError, match="media_credentials_selection_invalid"):
        build_media_reader(bucket="chirp-media", project="chirps-prod")


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("C436_WORKER_POSITIVE_IDS"), reason="requires disposable migrated worker database")
async def test_positive_pg_dispatch_updates_only_bound_obligation() -> None:
    ids = json.loads(os.environ["C436_WORKER_POSITIVE_IDS"])
    from app.config import get_settings
    get_settings.cache_clear()
    assert get_settings().account_deletion_deferred_cleanup_enabled is True
    scope = ids["scope"]
    binding = CleanupBinding(
        uuid.UUID(ids["obligation_id"]), uuid.UUID(ids["request_id"]),
        uuid.UUID(ids["user_id"]), "temporary_media", ids["scope_sha256"],
    )
    calls: list[uuid.UUID] = []

    async def provider(claim):
        calls.append(claim.id)
        return DeferredCleanupReceipt(
            category="temporary_media", provider="media_storage", subject=str(claim.user_id),
            request_id=str(claim.request_id), scope=scope, scope_sha256=claim.scope_sha256,
            pass_digest="1" * 64, readback=True, retained_with_reason=(), remaining_count=1,
        )

    counts = await run_one_batch(
        get_session_factory(), provider, binding,
        expected_role="c436_worker_test", expected_database="chirp_c436_worker_20261016",
        timeout_seconds=10,
    )
    assert counts == {"claimed": 1, "rescheduled": 1, "completed": 0, "uncertain": 0}
    assert calls == [binding.obligation_id]
    async with get_session_factory()() as session:
        rows = (await session.execute(text(
            "SELECT id, state, attempt_count, owner_token FROM account_post_deletion_cleanup ORDER BY id"
        ))).all()
    by_id = {str(row[0]): row for row in rows}
    target = by_id[str(binding.obligation_id)]
    foreign = by_id[ids["foreign_obligation_id"]]
    assert (target[1], target[2], target[3]) == ("pending", 1, None)
    assert (foreign[1], foreign[2], foreign[3]) == ("pending", 0, None)
