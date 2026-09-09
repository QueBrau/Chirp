"""Pins both directions of the skip guard added to test_ws_resource_integration.py (c394).

Board c92 established the pattern (see tests/test_ws_fanout.py's needs_redis): the
guard must be a CONNECTION check and nothing more, or it silently hides real
regressions. c394 applies the same pattern to tests/test_ws_resource_integration.py,
whose four Redis-backed tests otherwise ERROR permanently on a machine with no local
Redis (Jose's Mac). This file drives that target file as a real pytest subprocess,
twice, to prove the guard skips exactly when nothing is listening and never skips
merely because something went wrong talking to a live, non-Redis port.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
TARGET = "tests/test_ws_resource_integration.py"


def _closed_port() -> int:
    """Bind a loopback port, learn its number, release it - guarantees a refusal.

    Picking a hardcoded 'probably free' port would pass for the wrong reason on a
    machine where something happens to listen there.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _SilentHandler(socketserver.BaseRequestHandler):
    """Accepts a TCP connection and closes it without speaking a word of Redis."""

    def handle(self) -> None:
        self.request.close()


@contextlib.contextmanager
def _non_redis_listener():
    """A live loopback TCP listener that accepts connections but is not Redis.

    Proves the guard does not skip merely because something went wrong talking to
    the port - only when nothing is listening there at all.
    """
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SilentHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_target(redis_url: str) -> subprocess.CompletedProcess:
    # c361's CLI tests establish the pattern: start from the parent environment so
    # DATABASE_URL/TEST_DATABASE_URL pass through unchanged and the subprocess's
    # scratch DB is set up the same way the outer suite's is, then override only
    # what this test is about.
    env = dict(os.environ)
    env["REDIS_URL"] = redis_url
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-x", "-p", "no:cacheprovider", TARGET],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
    )


def test_redis_less_machine_skips_the_four_tests_with_a_named_reason():
    port = _closed_port()
    result = _run_target(f"redis://127.0.0.1:{port}/0")
    output = result.stdout + result.stderr

    assert "4 skipped" in output, output
    assert "error" not in output.lower(), output
    assert "failed" not in output.lower(), output
    assert result.returncode == 0, output
    # names the reason, not a bare skip
    assert "c394" in output, output


def test_open_non_redis_port_does_not_skip():
    with _non_redis_listener() as port:
        result = _run_target(f"redis://127.0.0.1:{port}/0")
    output = result.stdout + result.stderr

    assert "skipped" not in output.lower(), output
    assert ("error" in output.lower()) or ("failed" in output.lower()), output
    assert result.returncode != 0, output
