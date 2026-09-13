"""Every pre-accept WS rejection says which one it was (board c405 slice 1).

THE GAP. app/ws/gateway.py rejects at five points before accept(), and Starlette
turns any pre-accept close into a bare HTTP 403 with the close code discarded. So
from outside, a capacity failure, an expired token, an unknown user and a suspended
account are one indistinguishable event -- and none of them was recorded anywhere.
A forced reproduction on c405 showed a client seeing HTTP 403 while the real cause
was a sqlalchemy QueuePool TimeoutError that the bare `except Exception` threw away.

These tests pin the log line at each branch (the per-occurrence record) and the two
throttled counters (the rate). They assert NO behaviour change: every branch still
closes the same way it did, which is why each case also checks the client still sees
the same rejection.
"""
from __future__ import annotations

import asyncio

import pytest
import websockets

from app.core import operational_signals
from app.db import get_engine
from app.main import create_app
from app.ws import gateway
from loadtest.ws_resource_probe import local_server


@pytest.fixture(autouse=True)
def fresh_signal_throttle():
    """observe() throttles per event name per PROCESS for 60s, so without this the
    second test to emit a given signal would silently record nothing and its
    assertion would pass or fail for reasons unrelated to the code under test."""
    operational_signals._last_emitted.clear()
    yield
    operational_signals._last_emitted.clear()


def _reasons(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "ws reject reason=" in r.getMessage()]


async def _rejected(base, subprotocols=None):
    """Connect and require the pre-accept rejection the client actually sees."""
    with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
        await websockets.connect(
            base + "/ws", subprotocols=subprotocols, open_timeout=8,
            close_timeout=.2, proxy=None,
        )
    assert caught.value.response.status_code == 403, (
        "behaviour must not change: every pre-accept rejection is still a bare 403"
    )


async def test_no_credentials_says_so(caplog):
    caplog.set_level("WARNING")
    async with local_server(create_app()) as (base, _server):
        await _rejected(base)
    assert _reasons(caplog) == ["ws reject reason=no_credentials pre_accept=true"]


async def test_user_not_found_says_so(client, caplog):
    """Needs the migrated schema (via `client`): without it the lookup RAISES and the
    reconcile branch answers instead, which is a different branch and would make this
    test pass for the wrong reason."""
    caplog.set_level("WARNING")
    async with local_server(create_app()) as (base, _server):
        await _rejected(base, subprotocols=["uid-that-has-no-user-row"])
    assert _reasons(caplog) == ["ws reject reason=user_not_found pre_accept=true"]


async def test_auth_timeout_says_so(caplog, monkeypatch):
    caplog.set_level("WARNING")

    async def _never_returns(_websocket):
        await asyncio.Future()

    monkeypatch.setattr(gateway, "_resolve_uid", _never_returns)
    monkeypatch.setattr(gateway, "WS_AUTH_SECONDS", .05)
    async with local_server(create_app()) as (base, _server):
        await _rejected(base, subprotocols=["anything"])
    assert _reasons(caplog) == ["ws reject reason=auth_timeout pre_accept=true"]


async def test_suspended_says_so_names_journey_b_and_counts(client, make_user, caplog):
    """The branch with the user-visible consequence: the 4403 is discarded, so the
    client never reaches its suspension handling. Named journey=c405_b in the line
    so a reader lands on the card without re-deriving the chain."""
    from sqlalchemy import text

    from app.db import get_session_factory

    user = await make_user()
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET suspended_at = now() WHERE id = :id"), {"id": user.id}
        )
        await session.commit()

    caplog.set_level("WARNING")
    async with local_server(create_app()) as (base, _server):
        await _rejected(base, subprotocols=[user.firebase_uid])

    assert _reasons(caplog) == [
        "ws reject reason=account_suspended pre_accept=true journey=c405_b"
    ]
    assert "ws_connect_suspended_rejected" in operational_signals._last_emitted, (
        "the suspension branch must emit its countable signal"
    )


async def test_capacity_failure_is_named_and_counted_not_swallowed(client, make_user, caplog, monkeypatch):
    """A pool-checkout timeout must be distinguishable from an auth rejection, and
    its cause must survive.

    The exception is INJECTED rather than produced by really exhausting the pool.
    Slice 1's contract is "when the identity lookup raises a pool timeout, say so and
    count it"; really exhausting the pool is c405's reproduction of the underlying
    bug, and it is recorded on the card with its verbatim output. Injecting tests the
    contract deterministically, where the real-pool version depends on when the
    engine was built relative to the settings change and is exactly the kind of
    load-dependent setup that produced c403's unexplained reds.
    """
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    user = await make_user()

    def _pool_exhausted():
        raise SQLAlchemyTimeoutError(
            "QueuePool limit of size 1 overflow 0 reached, connection timed out, timeout 1.00"
        )

    monkeypatch.setattr(gateway, "get_session_factory", _pool_exhausted)
    caplog.set_level("WARNING")
    async with local_server(create_app()) as (base, _server):
        await _rejected(base, subprotocols=[user.firebase_uid])

    assert _reasons(caplog) == ["ws reject reason=identity_lookup_failed pre_accept=true"]
    assert "ws_connect_capacity_rejected" in operational_signals._last_emitted, (
        "a pool-checkout timeout must emit the capacity signal, not vanish"
    )
    causes = [
        r.exc_info[0].__name__ for r in caplog.records
        if "identity_lookup_failed" in r.getMessage() and r.exc_info
    ]
    assert causes == ["TimeoutError"], (
        f"the cause must survive rather than be discarded by the bare except: {causes}"
    )


async def test_a_non_capacity_lookup_failure_is_not_counted_as_capacity(client, make_user, caplog, monkeypatch):
    """The capacity counter must mean capacity. An ordinary lookup failure takes the
    same branch and must log there WITHOUT incrementing the capacity signal, or the
    metric would count unrelated faults."""
    user = await make_user()

    def _ordinary_failure():
        raise RuntimeError("something else entirely")

    monkeypatch.setattr(gateway, "get_session_factory", _ordinary_failure)
    caplog.set_level("WARNING")
    async with local_server(create_app()) as (base, _server):
        await _rejected(base, subprotocols=[user.firebase_uid])

    assert _reasons(caplog) == ["ws reject reason=identity_lookup_failed pre_accept=true"]
    assert "ws_connect_capacity_rejected" not in operational_signals._last_emitted, (
        "only a pool timeout may increment the capacity signal"
    )
