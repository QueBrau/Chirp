"""WS connect-storm leg: ramp connections, hold, classify every outcome."""
from __future__ import annotations

import asyncio
import time

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from loadtest.accounts import Manifest, VirtualUser
from loadtest.config import HarnessConfig
from loadtest.metrics import Recorder

OPEN_TIMEOUT_SECONDS = 10.0

# Server close codes from app/ws/gateway.py, named here so the report reads.
CLOSE_CODE_NAMES = {
    1000: "normal_close",
    4401: "auth_rejected",
    4403: "account_suspended",
    4503: "realtime_unavailable",
}


def _connect_kwargs(user: VirtualUser, auth_mode: str) -> dict:
    """Auth exactly as a real client would: emulated mode can set a header
    (this is a native caller), firebase mode offers the token as the
    subprotocol because that is the path the mobile app uses."""
    if auth_mode == "emulated":
        return {"additional_headers": {"X-Debug-Firebase-Uid": user.uid}}
    return {"subprotocols": [user.id_token]}


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
    try:
        connection = await websockets.connect(
            ws_url, open_timeout=OPEN_TIMEOUT_SECONDS, **_connect_kwargs(user, auth_mode)
        )
    except (OSError, InvalidHandshake, asyncio.TimeoutError, TimeoutError):
        # Handshake never completed: refused, timed out, or rejected pre-accept.
        # This is what ws_failure_pct counts; post-accept closes are not
        # failures to CONNECT and are reported by close code instead.
        return
    recorder.record_ws_connected((time.monotonic() - start) * 1000)
    deadline = time.monotonic() + hold_seconds
    close_code = 1000
    try:
        while time.monotonic() < deadline and not stop.is_set():
            remaining = deadline - time.monotonic()
            try:
                # The stream is server -> client; recv doubles as close detection.
                await asyncio.wait_for(connection.recv(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue
    except ConnectionClosed as closed:
        close_code = closed.rcvd.code if closed.rcvd else 1006
    finally:
        await connection.close()
    recorder.record_ws_close(close_code)


async def run_ws_phase(
    config: HarnessConfig, manifest: Manifest, recorder: Recorder, stop: asyncio.Event
) -> None:
    """Ramp at the configured rate to max_sockets, hold, close. The ramp rate is
    itself a cap: this leg cannot connect faster than the config allows."""
    interval = 1.0 / config.ws.connects_per_second
    tasks: list[asyncio.Task] = []
    for i in range(config.ws.max_sockets):
        if stop.is_set():
            break
        user = manifest.users[i % len(manifest.users)]
        tasks.append(
            asyncio.create_task(
                _one_socket(
                    config.ws_url, user, config.auth_mode, config.ws.hold_seconds, recorder, stop
                )
            )
        )
        await asyncio.sleep(interval)
    await asyncio.gather(*tasks, return_exceptions=True)
