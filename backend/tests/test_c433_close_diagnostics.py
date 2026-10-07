"""Opt-in, header-gated diagnostics for live WebSocket close probes."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import websockets
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

from app.config import get_settings
from app.ws import transport
from loadtest.ws_resource_probe import local_server

PROBE = "0123456789abcdef0123456789abcdef"
SECRET = "secret-canary-value"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _events(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == transport.logger.name and record.getMessage().startswith("{")
    ]


def _app():
    app = FastAPI()

    @app.websocket("/probe")
    async def probe(websocket: WebSocket):
        offered = websocket.scope.get("subprotocols", [])
        await websocket.accept(subprotocol=offered[0] if offered else None)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    return app


@pytest.mark.parametrize(
    "headers",
    [None, [("X-Chirp-Close-Probe", "0123456789ABCDEF0123456789ABCDEF")],
     [("X-Chirp-Close-Probe", "f" * 31)],
     [("X-Chirp-Close-Probe", "f" * 33)],
     [("X-Chirp-Close-Probe", "g" * 32)],
     [("X-Chirp-Close-Probe", PROBE), ("X-Chirp-Close-Probe", PROBE)]],
)
async def test_default_or_invalid_probe_header_is_silent(monkeypatch, caplog, headers):
    monkeypatch.setenv("WS_CLOSE_DIAGNOSTICS", "true")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger=transport.logger.name)
    async with local_server(_app()) as (base, _server):
        kwargs = {"proxy": None, "close_timeout": 1}
        if headers is not None:
            kwargs["additional_headers"] = headers
        async with websockets.connect(base + "/probe", **kwargs) as socket:
            await socket.close(code=1000, reason=SECRET)
            assert socket.close_code == 1000
    assert _events(caplog) == []


async def test_diagnostics_default_off_is_silent_for_valid_header(monkeypatch, caplog):
    monkeypatch.delenv("WS_CLOSE_DIAGNOSTICS", raising=False)
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger=transport.logger.name)
    async with local_server(_app()) as (base, _server):
        async with websockets.connect(
            base + "/probe", proxy=None, close_timeout=1,
            additional_headers={"X-Chirp-Close-Probe": PROBE},
        ) as socket:
            await socket.close(code=1000, reason=SECRET)
            assert socket.close_code == 1000
    assert _events(caplog) == []


async def test_valid_probe_records_real_peer_close_and_transport_lost(monkeypatch, caplog):
    monkeypatch.setenv("WS_CLOSE_DIAGNOSTICS", "true")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger=transport.logger.name)
    async with local_server(_app()) as (base, server):
        async with websockets.connect(
            base + "/probe", proxy=None, close_timeout=1,
            subprotocols=[SECRET],
            additional_headers={"X-Chirp-Close-Probe": PROBE, "Authorization": "Bearer " + SECRET},
        ) as socket:
            assert socket.subprotocol == SECRET
            protocol = next(iter(server.server_state.connections))
            await socket.send(SECRET)
            await socket.close(code=1000, reason=SECRET)
            assert socket.close_code == 1000
        # Repeat the diagnostic callbacks against the real closed adapter.
        # Removing the per-connection event guard must produce extra records.
        for _ in range(3):
            protocol._emit_close_probe("peer_close")
            protocol._emit_close_probe("transport_lost")
    events = _events(caplog)
    assert {event["phase"] for event in events} == {"peer_close", "transport_lost"}
    assert len(events) == 2
    assert all(event["probehex"] == PROBE for event in events)
    assert all(event["received_close_code"] == 1000 for event in events)
    assert all(event["sent_close_code"] == 1000 for event in events)
    assert all(event["state"] in {"OPEN", "CLOSING", "CLOSED"} for event in events)
    assert all(set(event) == {
        "event", "phase", "probehex", "received_close_code",
        "sent_close_code", "transport_closing", "state",
    } for event in events)
    encoded = " ".join(str(event).lower() for event in events)
    assert all(secret.lower() not in encoded for secret in (SECRET, "x-chirp-close-probe", "authorization"))
    assert all("exception" not in event for event in events)
    assert len({event["phase"] for event in events}) == len(events)


async def test_valid_probe_abrupt_eof_records_only_transport_lost(monkeypatch, caplog):
    monkeypatch.setenv("WS_CLOSE_DIAGNOSTICS", "true")
    caplog.set_level(logging.INFO, logger=transport.logger.name)
    disconnected = asyncio.Event()
    app = FastAPI()

    @app.websocket("/probe")
    async def probe(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            disconnected.set()

    async with local_server(app) as (base, _server):
        socket = await websockets.connect(
            base + "/probe", proxy=None, close_timeout=1,
            additional_headers={"X-Chirp-Close-Probe": PROBE},
        )
        socket.transport.abort()
        await asyncio.wait_for(disconnected.wait(), 2)
        await asyncio.sleep(0)
    events = _events(caplog)
    assert [event["phase"] for event in events] == ["transport_lost"]
    event = events[0]
    assert event["received_close_code"] is None
    assert event["sent_close_code"] is None
    assert event["probehex"] == PROBE


async def test_logging_failure_does_not_change_close(monkeypatch, caplog):
    monkeypatch.setenv("WS_CLOSE_DIAGNOSTICS", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(transport.logger, "info", lambda _message: (_ for _ in ()).throw(RuntimeError(SECRET)))
    async with local_server(_app()) as (base, _server):
        async with websockets.connect(
            base + "/probe", proxy=None, close_timeout=1,
            additional_headers={"X-Chirp-Close-Probe": PROBE},
        ) as socket:
            await socket.close(code=1000)
            assert socket.close_code == 1000
    assert SECRET not in caplog.text


def test_super_close_exception_propagates(monkeypatch):
    def boom(_self, _event):
        raise RuntimeError("sentinel")

    monkeypatch.setattr(WebSocketsSansIOProtocol, "handle_close", boom)
    protocol = object.__new__(transport.BoundedFragmentWebSocketsProtocol)
    with pytest.raises(RuntimeError, match="sentinel"):
        protocol.handle_close(object())


# ---------------------------------------------------------------------------
# c433 coverage holes, found by sabotaging this file rather than reading it
# (chirps-a2, Oct 7; Jose approved closing them before spending a test window).
#
# Every test above closes with code=1000 and asserts 1000 for BOTH recorded
# codes. That left two mutations of _close_code() completely undetected:
# hardcoding 1000, and swapping "close_rcvd" for "close_sent". Either one
# destroys the only thing these diagnostics exist to do, which is tell a normal
# 1000 close apart from the abnormal 1006 the c433 investigation is chasing --
# and the suite stayed green through both.
#
# One test per mutation, because the integration path cannot catch the swap:
# measured on real connections, this server ECHOES the peer's code, so received
# and sent are always equal there no matter which attribute is read.
# ---------------------------------------------------------------------------


async def test_valid_probe_records_a_non_1000_close_code(monkeypatch, caplog):
    """A non-1000 close is recorded as itself, not flattened to 1000.

    Kills the hardcode mutation. 1011 is used rather than 1006 on purpose: 1006
    is never carried in a close frame (it is what a peer infers from an abrupt
    EOF, covered separately above), so asserting it here would be asserting
    something the protocol cannot deliver.
    """
    monkeypatch.setenv("WS_CLOSE_DIAGNOSTICS", "true")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger=transport.logger.name)
    async with local_server(_app()) as (base, _server):
        async with websockets.connect(
            base + "/probe", proxy=None, close_timeout=1,
            additional_headers={"X-Chirp-Close-Probe": PROBE},
        ) as socket:
            await socket.close(code=1011, reason=SECRET)
            assert socket.close_code == 1011

    events = _events(caplog)
    assert {event["phase"] for event in events} == {"peer_close", "transport_lost"}
    # The assertion that matters: the recorded value tracks the ACTUAL code.
    assert all(event["received_close_code"] == 1011 for event in events), events
    assert all(event["sent_close_code"] == 1011 for event in events), events
    # And nothing in the payload fell back to the value every other test uses.
    assert not any(1000 in (event["received_close_code"], event["sent_close_code"])
                   for event in events), events


class _FakeClose:
    """Stands in for a websockets Close frame: only .code is read."""

    def __init__(self, code: int) -> None:
        self.code = code


class _FakeState:
    name = "CLOSED"


class _FakeConn:
    """A connection whose two close codes DIFFER, which a real one here never does."""

    def __init__(self, received: int, sent: int) -> None:
        self.close_rcvd = _FakeClose(received)
        self.close_sent = _FakeClose(sent)
        self.state = _FakeState()


async def test_received_and_sent_close_codes_are_not_interchanged(monkeypatch, caplog):
    """`received_close_code` reads close_rcvd and `sent_close_code` reads close_sent.

    Kills the swap mutation, which the integration tests above structurally
    cannot: this server echoes the peer's code, so on every real connection the
    two values are equal and reading the wrong attribute is invisible.

    Asymmetry is therefore constructed rather than waited for -- 1006 received
    against 1009 sent, the exact pair this investigation cares about: 1006 is
    the abnormal close under study and 1009 is what the fragment bound sends
    (app/ws/transport.py's handle_cont). A real connection reaching that state
    is precisely when the two codes diverge and when reading the wrong one
    would send someone chasing the wrong peer.
    """
    monkeypatch.setenv("WS_CLOSE_DIAGNOSTICS", "true")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger=transport.logger.name)
    async with local_server(_app()) as (base, server):
        async with websockets.connect(
            base + "/probe", proxy=None, close_timeout=1,
            additional_headers={"X-Chirp-Close-Probe": PROBE},
        ) as socket:
            protocol = next(iter(server.server_state.connections))
            await socket.send(SECRET)

            # Precondition, asserted rather than assumed: the probe really is
            # configured on this connection, so a later empty event list means
            # the mapping is broken and not that diagnostics were simply off.
            assert protocol._close_probe == PROBE

            caplog.clear()
            protocol.conn = _FakeConn(received=1006, sent=1009)
            # The per-connection guard is what the test above pins; clear it so
            # this emit is not swallowed as a repeat of a phase already seen.
            protocol._close_probe_events.clear()
            protocol._emit_close_probe("peer_close")

            events = _events(caplog)
            assert len(events) == 1, events
            event = events[0]
            assert event["received_close_code"] == 1006, event
            assert event["sent_close_code"] == 1009, event
            # Stated as the inequality too: equal values are exactly the blind
            # spot this test exists to remove.
            assert event["received_close_code"] != event["sent_close_code"], event
