"""The split's explicit worker setting governs the real existing lifespan task.

No provider/database access: only the outbox's I/O boundary is replaced. The
actual Settings parser, lifespan task creation and sweeper loop execute.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app import main
from app.services import outbox


@pytest.mark.parametrize("role,enabled", [("ws", "false"), ("api", "true")])
async def test_lifespan_obeys_explicit_outbox_owner(monkeypatch, role, enabled):
    monkeypatch.setenv("OUTBOX_SWEEPER_ENABLED", enabled)
    settings = Settings(_env_file=None, service_role=role, env="local", auth_mode="emulated")
    assert settings.outbox_sweeper_enabled is (enabled == "true")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    dispatched = asyncio.Event()

    async def dispatch(batch_limit):
        assert batch_limit == settings.outbox_sweep_batch_limit
        dispatched.set()

    dispatch_mock = AsyncMock(side_effect=dispatch)
    stats_mock = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr(outbox, "dispatch_pending", dispatch_mock)
    monkeypatch.setattr(outbox, "queue_stats", stats_mock)
    app = SimpleNamespace(state=SimpleNamespace())
    async with main.lifespan(app):
        if role == "api":
            await asyncio.wait_for(dispatched.wait(), timeout=1)
            assert dispatch_mock.await_count == 1
            assert stats_mock.await_count == 1
            assert not app.state.outbox_sweeper.done()
        else:
            await asyncio.wait_for(app.state.outbox_sweeper, timeout=1)
            dispatch_mock.assert_not_awaited()
            stats_mock.assert_not_awaited()
    assert app.state.outbox_sweeper.done()
