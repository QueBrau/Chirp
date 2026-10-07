"""Focused tests for the opt-in c433 peer-close grace path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.ws import transport
from loadtest.ws_resource_probe import local_server
import websockets


class _Loop:
    def __init__(self):
        self.calls = []

    def call_later(self, delay, callback):
        handle = SimpleNamespace(cancel=lambda: self.calls.append(("cancel", delay)))
        self.calls.append(("schedule", delay, callback))
        return handle


class _Transport:
    def __init__(self):
        self.writes = []
        self.closed = False

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True


def _protocol(code=1000, *, close_sent=False, closing=False):
    protocol = object.__new__(transport.BoundedFragmentWebSocketsProtocol)
    protocol.conn = SimpleNamespace(
        close_rcvd=SimpleNamespace(code=code, reason=""),
        data_to_send=lambda: [b"close-frame"],
    )
    protocol.close_sent = close_sent
    protocol.close_timer = None
    protocol.loop = _Loop()
    protocol.transport = _Transport()
    protocol.transport.closed = closing
    protocol.queue = SimpleNamespace(put_nowait=lambda value: protocol.queue_values.append(value))
    protocol.queue_values = []
    protocol._close_probe = None
    protocol._close_probe_events = set()
    return protocol


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_delay_is_default_off_and_env_opt_in(monkeypatch):
    monkeypatch.delenv("WS_PEER_CLOSE_DELAY_ENABLED", raising=False)
    assert get_settings().ws_peer_close_delay_enabled is False
    get_settings.cache_clear()
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    assert get_settings().ws_peer_close_delay_enabled is True


@pytest.mark.parametrize("code", [1001, 1006, 1009, 1011])
def test_non_normal_peer_close_keeps_stock_path(monkeypatch, code):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    protocol = _protocol(code)
    assert protocol._should_delay_peer_close() is False


def test_server_initiated_or_closing_transport_keeps_stock_path(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    assert _protocol(close_sent=True)._should_delay_peer_close() is False
    assert _protocol(closing=True)._should_delay_peer_close() is False


def test_enabled_normal_peer_close_writes_once_and_schedules_bounded_timer(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    protocol = _protocol()
    protocol._handle_delayed_peer_close()
    assert protocol.queue_values == [{"type": "websocket.disconnect", "code": 1000, "reason": ""}]
    assert protocol.transport.writes == [b"close-frame"]
    assert protocol.close_sent is True
    assert protocol.loop.calls[0][:2] == ("schedule", 0.1)


def test_reentrant_close_delegates_without_second_write(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    delegated = []
    monkeypatch.setattr(
        transport.WebSocketsSansIOProtocol,
        "handle_close",
        lambda self, event: delegated.append(event),
    )
    protocol = _protocol()
    protocol.handle_close("first")
    protocol.handle_close("second")
    assert protocol.transport.writes == [b"close-frame"]
    assert delegated == ["second"]


async def test_enabled_normal_peer_close_completes_over_real_loopback(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    app = FastAPI()

    @app.websocket("/close")
    async def close(ws: WebSocket):
        await ws.accept()
        await ws.receive_text()

    async with local_server(app) as (base, _server):
        async with websockets.connect(base + "/close", close_timeout=1, proxy=None) as socket:
            await socket.close(1000)
            assert socket.close_code == 1000


async def test_enabled_non1000_peer_close_uses_stock_path(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    app = FastAPI()

    @app.websocket("/close")
    async def close(ws: WebSocket):
        await ws.accept()
        await ws.receive_text()

    async with local_server(app) as (base, _server):
        async with websockets.connect(base + "/close", close_timeout=1, proxy=None) as socket:
            await socket.close(1011)
            assert socket.close_code == 1011


async def test_enabled_server_initiated_close_uses_stock_path(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    app = FastAPI()

    @app.websocket("/close")
    async def close(ws: WebSocket):
        await ws.accept()
        await ws.close(1011)

    async with local_server(app) as (base, _server):
        with pytest.raises(websockets.ConnectionClosed) as caught:
            async with websockets.connect(base + "/close", close_timeout=1, proxy=None) as socket:
                await socket.recv()
        assert caught.value.rcvd.code == 1011


async def test_enabled_abrupt_disconnect_uses_stock_path(monkeypatch):
    monkeypatch.setenv("WS_PEER_CLOSE_DELAY_ENABLED", "true")
    app = FastAPI()
    disconnected = asyncio.Event()

    @app.websocket("/close")
    async def close(ws: WebSocket):
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            disconnected.set()

    async with local_server(app) as (base, _server):
        socket = await websockets.connect(base + "/close", close_timeout=1, proxy=None)
        socket.transport.abort()
        await asyncio.wait_for(disconnected.wait(), 2)
