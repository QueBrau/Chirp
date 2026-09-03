"""c292: the limiter's Redis fallback must leave evidence, without becoming a flood.

Two problems with a silent fallback, and this file pins the fix for both.

OPERATIONAL: if Redis goes unreachable, every instance quietly keeps its own private
window. Nothing errors, nothing 500s, and no line appears anywhere - the limits simply
stop being shared, so the effective ceiling multiplies by the instance count and stays
that way indefinitely. A degradation with no evidence is one nobody fixes.

EVIDENTIAL: it is why c290's probe proved the limiter REFUSES in prod but could not
prove the count was SHARED. One instance served all 61 requests, and a single-instance
run looks identical whether it counted in Redis or in the fallback. With this warning,
the proof by elimination becomes available: probe while at least two instances serve,
and the ABSENCE of this line says Redis carried it. That later run is what these tests
make trustworthy - if the warning were unreliable, absence would prove nothing.

The flood guard is not a nicety either: a Redis outage under load means EVERY request
takes this branch, and one line per request would turn a degradation into a second
incident.
"""
from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.services import rate_limit


@pytest.fixture(autouse=True)
def _clean_limiter():
    rate_limit._reset_all()
    yield
    rate_limit._reset_all()


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch):
    """env=production so allow() takes the Redis path, and a Redis that always raises.

    allow() short-circuits to the local limiter when env == "local", which is what the
    suite normally runs as - so a test that forgot this would exercise nothing and pass.
    """
    # allow() re-imports get_settings INSIDE the function body, so the patch has to
    # land on app.config itself - patching the rate_limit module's namespace does
    # nothing, and a test that did so would short-circuit to the local limiter and pass
    # while exercising none of this. That is exactly how the first draft of this file
    # failed, which is the reason for this comment.
    import app.config as app_config

    settings = Settings(_env_file=None, env="production")  # type: ignore[call-arg]
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)

    import app.ws.pubsub as pubsub

    def _boom():
        raise ConnectionRefusedError("redis is unreachable")

    monkeypatch.setattr(pubsub, "get_redis", _boom)


async def test_the_fallback_is_no_longer_silent(
    redis_down, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole point: a degraded limiter says so."""
    with caplog.at_level(logging.WARNING, logger="app.services.rate_limit"):
        allowed = await rate_limit.allow("post_create:someone", max_calls=5, window_seconds=60)

    assert allowed is True, "the fallback must still ANSWER - degrading, not 500ing"
    messages = [r.getMessage() for r in caplog.records]
    assert any("fell back to the in-process window" in m for m in messages), messages
    assert any("no longer shared across instances" in m for m in messages), (
        "the line must say what it MEANS, not just that something happened"
    )


async def test_the_warning_names_the_scope_but_never_the_subject_or_the_error_text(
    redis_down, caplog: pytest.LogCaptureFixture
) -> None:
    """Keys are '<scope>:<subject>' and the subject is a user id, a client IP, or a
    student's email. The scope answers "which limiter degraded"; the subject would put
    PII in Cloud Logging to answer nothing.

    And never str(exc): a redis-py connection error's text can carry the connection URL,
    and REDIS_URL is a Secret Manager value. Same rule email_service already follows.
    """
    with caplog.at_level(logging.WARNING, logger="app.services.rate_limit"):
        await rate_limit.allow(
            "campus_verify_target:student@uncg.edu", max_calls=5, window_seconds=60
        )

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "campus_verify_target" in logged, "the scope is the useful half"
    assert "student@uncg.edu" not in logged, "a student's address must not reach the logs"
    assert "redis is unreachable" not in logged, (
        "str(exc) can carry the connection URL, which is a secret"
    )
    assert "ConnectionRefusedError" in logged, "the exception TYPE is the actionable part"


async def test_an_outage_under_load_produces_one_line_not_a_flood(
    redis_down, caplog: pytest.LogCaptureFixture
) -> None:
    """The failure this guard exists to prevent: every request takes the fallback branch
    during an outage, so an unguarded warning turns a degradation into a log incident."""
    with caplog.at_level(logging.WARNING, logger="app.services.rate_limit"):
        for i in range(200):
            await rate_limit.allow(f"post_create:user-{i}", max_calls=500, window_seconds=60)

    warnings = [r for r in caplog.records if "fell back" in r.getMessage()]
    assert len(warnings) == 1, (
        f"200 fallbacks produced {len(warnings)} warnings; the suppressor is not holding"
    )


async def test_a_persistent_outage_keeps_warning_after_the_interval(
    redis_down, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppressed is not silenced. A single line at the start of a long outage scrolls
    away and the next reader sees nothing, so the heartbeat has to resume."""
    with caplog.at_level(logging.WARNING, logger="app.services.rate_limit"):
        await rate_limit.allow("post_create:a", max_calls=5, window_seconds=60)

        # Move the process clock past the suppression interval.
        real_monotonic = rate_limit.time.monotonic
        offset = rate_limit.FALLBACK_WARNING_INTERVAL_SECONDS + 1
        monkeypatch.setattr(
            rate_limit.time, "monotonic", lambda: real_monotonic() + offset
        )
        await rate_limit.allow("post_create:b", max_calls=5, window_seconds=60)

    warnings = [r for r in caplog.records if "fell back" in r.getMessage()]
    assert len(warnings) == 2, f"expected a resumed heartbeat, got {len(warnings)}"


async def test_a_healthy_limiter_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The absence of the line is the evidence c290's elimination proof will rest on, so
    a healthy path emitting it even once would make that proof worthless."""
    with caplog.at_level(logging.WARNING, logger="app.services.rate_limit"):
        for _ in range(20):
            await rate_limit.allow("post_create:x", max_calls=100, window_seconds=60)

    assert [r for r in caplog.records if "fell back" in r.getMessage()] == []


def test_reset_all_clears_the_suppressor_too() -> None:
    """Process-global state bleeds between tests exactly like _WINDOWS does: without
    this, the first test to trip the fallback would silence every later one and they
    would pass for the wrong reason."""
    rate_limit._last_fallback_warning_at = 12345.0
    rate_limit._reset_all()
    assert rate_limit._last_fallback_warning_at is None
