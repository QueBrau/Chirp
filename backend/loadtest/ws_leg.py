"""Paced WS attempts: upgrade, exact application ACK, full hold, bounded cleanup."""
from __future__ import annotations

import asyncio
import json
import time

from websockets.asyncio.client import connect as WebSocketConnect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.protocol import State

from loadtest.accounts import Manifest, VirtualUser
from loadtest.config import HarnessConfig
from loadtest.metrics import Recorder

OPEN_TIMEOUT_SECONDS = 10.0
READY_TIMEOUT_SECONDS = 15.0
CLOSE_TIMEOUT_SECONDS = 1.0
MAX_RECEIVE_BYTES = 128 * 1024
RECEIVE_QUEUE_HIGH_WATER = 4


class TargetOnlyConnect(WebSocketConnect):
    """A configured target never grants permission to follow its HTTP redirects.

    This pinned modern-client hook returns the original handshake exception;
    it doesn't alter approval of a directly configured non-local target.
    """

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


connect = TargetOnlyConnect

CLOSE_CODE_NAMES = {
    1000: "normal_close",
    1006: "abnormal_no_received_close",
    4401: "auth_rejected",
    4403: "account_suspended",
    4503: "realtime_unavailable",
}


def _connect_kwargs(user: VirtualUser, auth_mode: str) -> dict:
    if auth_mode == "emulated":
        return {"additional_headers": {"X-Debug-Firebase-Uid": user.uid}}
    return {"subprotocols": [user.id_token]}


def _is_ready(frame: str | bytes) -> bool:
    if not isinstance(frame, str):
        return False
    try:
        # Pairs retain duplicate JSON keys: only one exact type/ready member is valid.
        return json.loads(frame, object_pairs_hook=list) == [("type", "ready")]
    except (ValueError, RecursionError):
        return False


async def _one_socket(
    ws_url: str,
    user: VirtualUser,
    auth_mode: str,
    hold_seconds: float,
    recorder: Recorder,
    stop: asyncio.Event,
) -> None:
    recorder.record_ws_attempt()
    start = time.monotonic()
    connection = None
    ready_key = None
    outcome = "internal_error"
    cleanup_timeout = False
    try:
        try:
            connection = await connect(
                ws_url, open_timeout=OPEN_TIMEOUT_SECONDS, close_timeout=CLOSE_TIMEOUT_SECONDS,
                max_size=MAX_RECEIVE_BYTES, max_queue=RECEIVE_QUEUE_HIGH_WATER,
                compression=None, proxy=None, **_connect_kwargs(user, auth_mode),
            )
        except TimeoutError:
            outcome = "handshake_timeout"
            return
        except (OSError, InvalidHandshake):
            outcome = "handshake_error"
            return
        recorder.record_ws_connected((time.monotonic() - start) * 1000)
        try:
            async with asyncio.timeout(READY_TIMEOUT_SECONDS):
                frame = await connection.recv()
        except TimeoutError:
            outcome = "ready_timeout"
            return
        if not _is_ready(frame):
            outcome = "invalid_ready"
            return
        # A buffered ACK followed by an already-parsed close is not a live ready socket.
        if connection.state is not State.OPEN:
            outcome = "closed_before_ready"
            return
        ready_key = recorder.record_ws_ready(
            (time.monotonic() - start) * 1000, lambda: connection.state is State.OPEN
        )
        try:
            async with asyncio.timeout(hold_seconds):
                while True:
                    await connection.recv()  # Drain, but there is no receipt/publisher contract.
                    await asyncio.sleep(0)  # Preserve deadlines even with a continuously full buffer.
        except TimeoutError:
            outcome = "completed_hold" if connection.state is State.OPEN else "closed_during_hold"
    except ConnectionClosed:
        outcome = "closed_before_ready" if ready_key is None else "closed_during_hold"
    except asyncio.CancelledError:
        outcome = "stopped" if stop.is_set() else "cancelled"
        raise
    finally:
        if ready_key is not None:
            recorder.end_ws_ready(ready_key)
        try:
            if connection is not None:
                try:
                    # Modern websockets is cancellation-responsive here. Its native timeout
                    # handles normal TCP closure; this outer budget also covers drain().
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        await connection.close()
                except TimeoutError:
                    cleanup_timeout = True
                    if outcome == "completed_hold":
                        outcome = "cleanup_timeout"
                finally:
                    if connection.state is not State.CLOSED:
                        connection.transport.abort()
                    # close_code remains None until CLOSED in the pinned modern
                    # protocol. Preserve a received code even if TCP cleanup stalls;
                    # never copy the peer's arbitrary reason into the report.
                    received = connection.protocol.close_rcvd
                    recorder.record_ws_close(received.code if received else (connection.close_code or 1006))
        except asyncio.CancelledError:
            outcome = "stopped" if stop.is_set() else "cancelled"
            raise
        except Exception:
            outcome = "internal_error"
            raise
        finally:
            recorder.record_ws_outcome(outcome, cleanup_timeout=cleanup_timeout)


async def run_ws_phase(
    config: HarnessConfig, manifest: Manifest, recorder: Recorder, stop: asyncio.Event
) -> None:
    """Finite cohort; normal HTTP completion doesn't truncate these per-ready holds."""
    tasks: list[asyncio.Task] = []

    async def stop_sockets():
        await stop.wait()
        for task in tasks:
            task.cancel()

    stopper = asyncio.create_task(stop_sockets(), name="loadtest-ws-stop")
    try:
        async with asyncio.TaskGroup() as group:
            for i in range(config.ws.max_sockets):
                if stop.is_set():
                    break
                user = manifest.users[i % len(manifest.users)]
                tasks.append(group.create_task(
                    _one_socket(config.ws_url, user, config.auth_mode,
                                config.ws.hold_seconds, recorder, stop),
                    name="loadtest-ws-socket",
                ))
                if i + 1 < config.ws.max_sockets:
                    try:
                        await asyncio.wait_for(stop.wait(), 1.0 / config.ws.connects_per_second)
                    except TimeoutError:
                        pass
    finally:
        stopper.cancel()
        try:
            await stopper
        except asyncio.CancelledError:
            pass
