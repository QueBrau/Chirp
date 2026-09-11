"""c354: inbound continuation-fragment count bound, at the real uvicorn wire parser.

Starlette TestClient / httpx's ASGI transport never exercises the real uvicorn
wire-protocol parser -- handle_cont only runs when actual bytes flow through
data_received(). So, like the repo's own diagnostic
(loadtest/ws_resource_probe.py:fragment_bookkeeping_evidence, which this harness
deliberately mirrors), these tests construct a real protocol instance against a
mock asyncio.Transport and feed it raw HTTP-upgrade + WS frame bytes directly.

Every test is parametrized over BOTH the shipped BoundedFragmentWebSocketsProtocol
and the stock WebSocketsSansIOProtocol under the identical frame sequence
(chirps-17 condition 1), so the fix is proven by contrast rather than assumed:
the stock class is shown NOT closing and retaining MORE fragments than the bound
under the same input that makes the bounded class close at exactly the bound.

The bounded protocol is constructed via uvicorn.Config(app, **container_transport_
options()) + config.load(), i.e. the exact --ws string backend/Dockerfile ships,
not a hand-typed import path -- a typo in the Dockerfile CMD would fail this file
the same way it fails test_ws_resource_integration.py::
test_real_container_cli_selects_effective_frame_limit.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from starlette.websockets import WebSocket
import uvicorn
from uvicorn.server import ServerState
from websockets.frames import Frame, Opcode

from app.ws.transport import WS_MAX_CONTINUATION_FRAMES, BoundedFragmentWebSocketsProtocol
from loadtest.ws_resource_probe import container_transport_options

BOUNDED = "bounded"
STOCK = "stock"


class _Transport(asyncio.Transport):
    """Minimal mock transport -- same shape as the repo's own diagnostic uses."""

    closed = False

    def get_extra_info(self, name, default=None):
        return ("127.0.0.1", 12345) if name in ("sockname", "peername") else default

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    def write(self, data):
        pass

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass


_HANDSHAKE = (
    b"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
    b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Version: 13\r\n\r\n"
)


@asynccontextmanager
async def _protocol(kind: str):
    """A real accepted WS connection at the protocol layer, for `kind` in {BOUNDED, STOCK}.

    `kind == BOUNDED` loads the config the exact way the shipped Dockerfile CMD
    would (container_transport_options() reads the Dockerfile), so this proves
    the shipped class. `kind == STOCK` forces uvicorn's own stock class over the
    same options, as the known-broken baseline for contrast.
    """
    accepted, finish = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        ws = WebSocket(scope, receive, send)
        await ws.accept()
        accepted.set()
        await finish.wait()

    options = container_transport_options()
    if kind == STOCK:
        options = options | {"ws": "websockets-sansio"}
    config = uvicorn.Config(app, **options, log_config=None, access_log=False, ws_ping_interval=None)
    config.load()
    if kind == BOUNDED:
        assert config.ws_protocol_class is BoundedFragmentWebSocketsProtocol, (
            "container_transport_options() no longer resolves to the app's bounded class "
            "-- the Dockerfile CMD's --ws value and this test have drifted"
        )
    else:
        assert config.ws_protocol_class.__name__ == "WebSocketsSansIOProtocol"
    state = ServerState()
    protocol = config.ws_protocol_class(config=config, server_state=state, app_state={})
    transport = _Transport()
    protocol.connection_made(transport)
    protocol.data_received(_HANDSHAKE)
    await asyncio.wait_for(accepted.wait(), 1)
    try:
        yield protocol, transport
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(*list(state.tasks), return_exceptions=True), 1)
        protocol.connection_lost(None)


def _send_text_then_cont(protocol, n_cont: int, *, final_fin: bool = False) -> None:
    protocol.data_received(Frame(Opcode.TEXT, b"a", fin=False).serialize(mask=True))
    for i in range(n_cont):
        is_last = final_fin and i == n_cont - 1
        protocol.data_received(Frame(Opcode.CONT, b"", fin=is_last).serialize(mask=True))


@pytest.mark.parametrize("kind", [BOUNDED, STOCK])
async def test_fragments_within_bound_are_delivered_to_app(kind):
    # Exactly WS_MAX_CONTINUATION_FRAMES - 1 (63) CONT frames, then one final CONT
    # with FIN completing the message: the exact boundary of "still allowed" for
    # the bounded class (guard fires at >= 64 retained, so 63 stays under it),
    # not an arbitrary small number. True for the stock class too, since it never
    # bounds at all.
    async with _protocol(kind) as (protocol, transport):
        _send_text_then_cont(protocol, WS_MAX_CONTINUATION_FRAMES - 1, final_fin=True)
        event = protocol.queue.get_nowait()
        assert event == {"type": "websocket.receive", "text": "a"}
        assert transport.closed is False
        assert protocol.close_sent is False


@pytest.mark.parametrize("kind", [BOUNDED, STOCK])
async def test_fragments_at_bound_close_1009_and_frame_list_stays_bounded(kind):
    # Exactly WS_MAX_CONTINUATION_FRAMES (64) CONT frames, FIN never sent: the
    # exact boundary where the bounded guard must fire.
    async with _protocol(kind) as (protocol, transport):
        _send_text_then_cont(protocol, WS_MAX_CONTINUATION_FRAMES, final_fin=False)
        if kind == BOUNDED:
            assert transport.closed is True
            assert protocol.conn.close_sent is not None
            assert protocol.conn.close_sent.code == 1009
            assert len(protocol.frames) <= WS_MAX_CONTINUATION_FRAMES
        else:
            # The known-open gap this fix closes: same 64-CONT sequence, stock
            # neither closes nor bounds the list -- it retains MORE than the
            # bound (1 TEXT + 64 CONT = 65 entries), proven by contrast against
            # the BOUNDED branch above under the identical frame sequence.
            assert transport.closed is False
            assert protocol.close_sent is False
            assert len(protocol.frames) > WS_MAX_CONTINUATION_FRAMES
            assert len(protocol.frames) == WS_MAX_CONTINUATION_FRAMES + 1


@pytest.mark.parametrize("kind", [BOUNDED, STOCK])
async def test_frames_after_close_do_not_keep_growing(kind):
    # Continue from the exact state test 2 produces, then feed 50 MORE empty
    # CONT frames.
    async with _protocol(kind) as (protocol, transport):
        _send_text_then_cont(protocol, WS_MAX_CONTINUATION_FRAMES, final_fin=False)
        length_at_bound = len(protocol.frames)
        for _ in range(50):
            protocol.data_received(Frame(Opcode.CONT, b"", fin=False).serialize(mask=True))
        if kind == BOUNDED:
            # Proves the connection is actually torn down at the protocol layer
            # (matching the original bug report's "the transport stayed open"
            # finding), not just that a close frame was queued once: further
            # post-close bytes do not keep growing the list.
            assert len(protocol.frames) == length_at_bound
            assert transport.closed is True
        else:
            # Contrast: on the stock class, the SAME extra 50 frames keep
            # growing the list without limit -- this is the exact unbounded
            # growth WEBSOCKET-RESOURCE-LIMITS.md measured (1001 then 2001
            # retained entries), reproduced here at a smaller, CI-bounded scale.
            assert len(protocol.frames) == length_at_bound + 50
            assert transport.closed is False


async def test_base_class_surface_matches_what_the_override_relies_on():
    """chirps-17 condition 3, instance-level half (see app/ws/transport.py for
    the class-level half, asserted at import time).

    BoundedFragmentWebSocketsProtocol.handle_cont reads self.frames, calls
    self.conn.send_close(code) and self.handle_parser_exception(), and that
    last call reads self.conn.close_sent.code/.reason. If the installed base
    class ever stopped setting any of these up the same way, this fails here
    instead of only at a random runtime close.
    """
    async with _protocol(BOUNDED) as (protocol, _transport):
        assert isinstance(protocol.frames, list)
        assert callable(protocol.conn.send_close)
        assert callable(protocol.handle_parser_exception)
        assert protocol.conn.close_sent is None  # not yet closed at this point
        protocol.conn.send_close(1009)
        assert protocol.conn.close_sent is not None
        assert protocol.conn.close_sent.code == 1009
