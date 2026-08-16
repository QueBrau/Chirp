"""Startup must SAY whether Redis is reachable (board c61).

The failure this guards against is not a crash, it is silence. Redis was never
provisioned in chirps-prod — no instance, no VPC connector, no REDIS_URL — and
nothing anywhere said so. The only symptom was per-connection (c62), which reads
as flaky clients rather than as missing infrastructure. A missing dependency
should announce itself once at boot, in a line you can grep for in Cloud Run.

These call _probe_redis directly rather than booting the app. Booting with
env="production" trips the unrelated guard in create_app that requires
auth_mode="firebase", which would drag real Firebase credentials into a test
about Redis. The probe is the unit under test; the lifespan simply awaits it.
"""
from __future__ import annotations

import contextlib
import logging
import socket

import pytest

from app.config import Settings
from app.main import _probe_redis


class _Capture(logging.Handler):
    """Collect records straight off the app.main logger.

    Deliberately not pytest's caplog: caplog attaches to the root logger and
    depends on propagation and on the global level, both of which earlier tests
    in the suite mutate (TestClient boots, uvicorn's log config, the WS token
    redaction filter). This test passed alone and failed in a full run for
    exactly that reason. Owning the handler makes the assertion independent of
    whatever logging state arrives from the rest of the suite.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, min_level: int = logging.DEBUG) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= min_level]


@contextlib.contextmanager
def capture_app_main():
    """Attach a handler to app.main for the duration of the block."""
    logger = logging.getLogger("app.main")
    handler = _Capture()
    previous_level, previous_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


def _dead_url() -> str:
    """A Redis URL whose port nothing listens on, so connecting is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return f"redis://127.0.0.1:{port}/0"


@pytest.fixture(autouse=True)
def reset_redis_client():
    """The pubsub client is a module global cached across calls."""
    import app.ws.pubsub as pubsub_module

    saved = pubsub_module._client
    pubsub_module._client = None
    yield
    pubsub_module._client = saved


async def test_unreachable_redis_logs_an_explicit_error() -> None:
    """A dead Redis produces one greppable error that names what is broken."""
    settings = Settings(env="production", auth_mode="firebase", redis_url=_dead_url())

    with capture_app_main() as log:
        await _probe_redis(settings)

    errors = log.messages(logging.ERROR)
    assert any("redis unreachable at startup" in m for m in errors), errors

    message = next(m for m in errors if "redis unreachable at startup" in m)
    # It has to say what is actually broken and where to look, not merely that
    # something failed — the whole point is that the last outage said nothing.
    assert "real-time fan-out is DOWN" in message
    assert "REDIS_URL" in message
    assert "4503" in message


async def test_probe_never_raises() -> None:
    """The probe must not be fatal, even though Firebase's init above it is.

    Load-bearing: making this raise would be an easy 'improvement' that converts
    a missing optional dependency into a total outage. Nothing in the app opens
    a websocket yet (c63), so a Redis outage must never stop the HTTP API from
    serving.
    """
    settings = Settings(env="production", auth_mode="firebase", redis_url=_dead_url())

    with capture_app_main():
        result = await _probe_redis(settings)

    assert result is None


async def test_probe_does_not_leak_the_url() -> None:
    """A Redis URL can carry a password, so it must never reach the logs.

    Memorystore AUTH and every managed Redis put the credential in the URL, so
    the moment this is pointed at a real instance the naive version of this log
    line would write that credential to Cloud Run.
    """
    settings = Settings(
        env="production",
        auth_mode="firebase",
        redis_url="redis://:sup3r-s3cret@127.0.0.1:6399/0",
    )

    with capture_app_main() as log:
        await _probe_redis(settings)

    joined = " ".join(log.messages())
    assert "sup3r-s3cret" in settings.redis_url  # the fixture is what we think it is
    assert "sup3r-s3cret" not in joined


async def test_probe_is_silent_in_local_env() -> None:
    """Local dev without Redis must not log an error on every boot.

    A warning that fires on every developer's machine is one people learn to
    scroll past, which is exactly how the real one would get missed.
    """
    settings = Settings(env="local", redis_url=_dead_url())

    with capture_app_main() as log:
        await _probe_redis(settings)

    assert not [m for m in log.messages() if "redis" in m.lower()]
