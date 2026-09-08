"""Local-only c354 Redis/Uvicorn resource probe and reusable test boundaries.

Run from backend with explicit disposable database and loopback Redis URLs. The
pytest fixture creates/drops its own marked database; no existing app database or
real provider is used. This module is a developer tool, not part of the image.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from click.testing import CliRunner
import uvicorn


def require_loopback(url: str) -> str:
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("the resource probe requires an explicit loopback endpoint")
    return url


def container_transport_options() -> dict:
    """Parse the real Docker CMD through installed Uvicorn's CLI, without serving.

    A guessed Config does not prove that the shipped command accepts or applies
    an option. Capturing Click's actual run arguments covers that boundary too.
    """
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    line = next(line for line in dockerfile.read_text().splitlines() if line.startswith("CMD exec uvicorn "))
    args = shlex.split(line.removeprefix("CMD exec uvicorn ").replace("${PORT}", "8080"))
    entry = importlib.import_module("uvicorn.main")
    original_run = entry.run
    captured = {}
    try:
        entry.run = lambda app, **kwargs: captured.update(kwargs)
        result = CliRunner().invoke(entry.main, args)
    finally:
        entry.run = original_run
    if result.exit_code:
        raise AssertionError(f"container Uvicorn CLI rejected arguments: {result.output}")
    return {key: captured[key] for key in ("ws", "ws_max_size", "ws_per_message_deflate")}


@asynccontextmanager
async def local_server(app, **options):
    """A real loopback listener using the actual container websocket settings."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off", log_level="critical",
        access_log=False, timeout_graceful_shutdown=1,
        **(container_transport_options() | options),
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]), name="probe-uvicorn")
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done(): await task
                await asyncio.sleep(.005)
        yield f"ws://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 3)
        finally:
            listener.close()


async def redis_totals(redis) -> dict:
    clients, memory = await asyncio.gather(redis.client_list(), redis.info("memory"))
    pubsub = [client for client in clients if "P" in client.get("flags", "")]
    return {
        "redis_clients": len(clients), "redis_pubsub_clients": len(pubsub),
        "redis_subscriptions": sum(int(client.get("sub", 0)) for client in pubsub),
        "redis_pubsub_output_bytes": sum(int(client.get("omem", 0)) for client in pubsub),
        "redis_used_memory_bytes": memory["used_memory"],
        "redis_rss_bytes": memory["used_memory_rss"],
    }


def process_rss_bytes() -> int | None:
    """Current RSS, separately from Python's traced heap; no capacity inference."""
    try:
        return 1024 * int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def record_result(result: dict) -> None:
    print("WS_RESOURCE_MEASUREMENT " + json.dumps(result, sort_keys=True), flush=True)
    output = os.environ.get("CHIRP_WS_REPORT")
    if output:
        with Path(output).open("a") as stream:
            stream.write(json.dumps(result, sort_keys=True) + "\n")


async def fragment_bookkeeping_evidence() -> None:
    """Bounded diagnostic for an OPEN transport issue, not a passing safety test.

    Actual pinned parser and ASGI task, controlled asyncio.Transport, no network.
    An upstream repair may make observed=False; neither result is hidden by a
    deliberately failing normal-CI test or treated as a whole-memory bound.
    """
    from starlette.websockets import WebSocket
    from uvicorn.server import ServerState
    from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
    from websockets.frames import Frame, Opcode

    class Transport(asyncio.Transport):
        closed = False
        def get_extra_info(self, name, default=None):
            return ("127.0.0.1", 12345) if name in ("sockname", "peername") else default
        def is_closing(self): return self.closed
        def close(self): self.closed = True
        def write(self, data): pass
        def pause_reading(self): pass
        def resume_reading(self): pass

    accepted, finish = asyncio.Event(), asyncio.Event()
    async def app(scope, receive, send):
        ws = WebSocket(scope, receive, send)
        await ws.accept()
        accepted.set()
        await finish.wait()
    config = uvicorn.Config(app, **container_transport_options(), log_config=None, access_log=False, ws_ping_interval=None)
    state = ServerState()
    protocol = WebSocketsSansIOProtocol(config=config, server_state=state, app_state={})
    transport = Transport()
    protocol.connection_made(transport)
    protocol.data_received(
        b"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    await asyncio.wait_for(accepted.wait(), .5)
    try:
        protocol.data_received(Frame(Opcode.TEXT, b"a", fin=False).serialize(mask=True))
        snapshots = []
        batch = b"".join(Frame(Opcode.CONT, b"", fin=False).serialize(mask=True) for _ in range(1000))
        for _ in range(2):
            protocol.data_received(batch)
            snapshots.append({
                "retained_fragments": len(protocol.frames), "message_payload_bytes": sum(map(len, protocol.frames)),
                "read_paused": protocol.read_paused, "transport_closed": transport.closed,
                "parser_error": protocol.conn.parser_exc is not None,
            })
        record_result({"case": "OPEN_empty_fragment_bookkeeping", "continuation_limit_in_probe": 2000,
            "known_limitation_observed": snapshots[-1]["retained_fragments"] > snapshots[0]["retained_fragments"], "snapshots": snapshots})
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(*list(state.tasks)), .5)
        protocol.connection_lost(None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--sockets", type=int, default=16, choices=range(1, 33), metavar="1..32")
    parser.add_argument("--rounds", type=int, default=3, choices=range(1, 6), metavar="1..5")
    parser.add_argument("--hold-seconds", type=float, default=38)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--include-fragment-evidence", action="store_true", help="also record the bounded actual-parser diagnostic for the open inbound issue")
    args = parser.parse_args()
    require_loopback(args.redis_url)
    require_loopback(args.database_url)
    if not urlsplit(args.database_url).path.endswith("/chirp_test"):
        parser.error("database URL must select the disposable chirp_test base")
    if not 0 < args.hold_seconds <= 60:
        parser.error("hold-seconds must be >0 and <=60")
    os.environ.update({
        "TEST_DATABASE_URL": args.database_url, "REDIS_URL": args.redis_url,
        "CHIRP_REQUIRE_DB": "1", "CHIRP_WS_SOCKETS": str(args.sockets),
        "CHIRP_WS_ROUNDS": str(args.rounds), "CHIRP_WS_HOLD_SECONDS": str(args.hold_seconds),
        "CHIRP_WS_REPORT": str(args.report),
    })
    if args.include_fragment_evidence:
        asyncio.run(fragment_bookkeeping_evidence())
    # This Intel macOS environment crashes importing native readline in pytest.
    sys.modules["readline"] = None
    import pytest
    return pytest.main(["-q", "-s", "tests/test_ws_resource_integration.py", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
