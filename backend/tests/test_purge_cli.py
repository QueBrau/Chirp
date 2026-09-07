"""Purge CLI rejects unsafe input before database access and reports partial work honestly."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.jobs import purge
from tests.conftest import MakeChapterWith
from tests.test_purge import _insert_post, _row_exists


@pytest.mark.parametrize("argv", [
    ["--batch-size", "0"], ["--max-batches", "-1"], ["--max-seconds", "0"],
    ["--retention-days", "0"], ["--retention-days", "-30"],
    ["--retention-days", "10000000000000"], ["--max-seconds", "nan"],
    ["--dry-run", "--apply"],
])
def test_invalid_cli_never_opens_database(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_factory():
        pytest.fail("invalid CLI reached database construction")

    monkeypatch.setattr("app.db.get_session_factory", forbidden_factory)
    with pytest.raises(SystemExit) as exc:
        purge.main(argv)
    assert exc.value.code == 2


def test_invalid_setting_retention_never_opens_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(purge, "get_settings", lambda: SimpleNamespace(purge_retention_days=0))
    monkeypatch.setattr("app.db.get_session_factory", lambda: pytest.fail("database opened"))
    with pytest.raises(SystemExit) as exc:
        purge.main([])
    assert exc.value.code == 2


def test_default_cli_is_preview(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    received: dict[str, object] = {}

    async def job(**kwargs):
        received.update(kwargs)
        return {"status": "preview", "physical_rows": 12}

    monkeypatch.setattr(purge, "run_purge_job", job)
    purge.main([])
    assert received["apply"] is False
    assert received["batch_size"] == 100
    assert received["max_batches"] == 10
    assert received["max_seconds"] == 120
    assert json.loads(capsys.readouterr().out)["status"] == "preview"


def test_incomplete_cli_is_nonzero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    async def job(**kwargs):
        assert kwargs["apply"] is True
        return {"status": "incomplete", "batches_committed": 1, "physical_rows": 100}

    monkeypatch.setattr(purge, "run_purge_job", job)
    with pytest.raises(SystemExit) as exc:
        purge.main(["--apply"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["physical_rows"] == 100


@pytest.mark.parametrize("retention", [0, -1, True, 1.5])
async def test_helper_rejects_invalid_retention_without_db_work(retention) -> None:
    class NoDatabase:
        def __getattr__(self, _name):
            pytest.fail("invalid retention attempted database work")

    with pytest.raises(ValueError, match="positive integer"):
        await purge.purge_expired_soft_deletes(NoDatabase(), retention_days=retention)


async def test_deadline_keeps_previously_committed_counts(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    posts = [await _insert_post(setup.chapter_id, setup.president.id,
                                deleted_at=now - timedelta(days=31)) for _ in range(2)]
    real_batch = purge.purge_batch
    calls = 0

    async def delayed_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.sleep(5)
        return await real_batch(*args, **kwargs)

    monkeypatch.setattr(purge, "purge_batch", delayed_batch)
    report = await purge.run_purge_job(
        apply=True, now=now, batch_size=1, max_batches=10, max_seconds=1,
    )
    assert report["status"] == "timed_out"
    assert report["batches_committed"] == 1
    assert report["physical_rows"] == 1
    assert report["commit_outcome_unknown"] is False
    assert sum([await _row_exists("posts", post) for post in posts]) == 1


async def test_failure_preserves_commits_and_hides_exception_contents(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    for _ in range(2):
        await _insert_post(setup.chapter_id, setup.president.id,
                           deleted_at=now - timedelta(days=31))
    real_batch = purge.purge_batch
    calls = 0

    async def failing_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("sensitive SQL parameter must stay out of job logs")
        return await real_batch(*args, **kwargs)

    monkeypatch.setattr(purge, "purge_batch", failing_batch)
    report = await purge.run_purge_job(apply=True, now=now, batch_size=1, max_batches=3)
    assert report["status"] == "failed"
    assert report["error_type"] == "RuntimeError"
    assert report["batches_committed"] == 1
    assert report["physical_rows"] == 1
    assert "sensitive SQL parameter" not in json.dumps(report)


async def test_deadline_during_commit_reports_unknown_outcome(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    post = await _insert_post(setup.chapter_id, setup.president.id,
                              deleted_at=now - timedelta(days=31))
    real_commit = AsyncSession.commit

    async def delayed_acknowledgement(session):
        await real_commit(session)
        await asyncio.sleep(5)

    monkeypatch.setattr(AsyncSession, "commit", delayed_acknowledgement)
    report = await purge.run_purge_job(apply=True, now=now, batch_size=1, max_seconds=1)
    assert report["status"] == "timed_out"
    assert report["commit_outcome_unknown"] is True
    assert report["batches_committed"] == 0
    assert report["physical_rows"] == 0  # Acknowledged commits only, not a rollback promise.
    assert not await _row_exists("posts", post)
