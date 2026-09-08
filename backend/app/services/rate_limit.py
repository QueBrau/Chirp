"""Small distributed rate limiter with a local fallback.

Production uses Redis fixed windows so Cloud Run instances share a budget. Local and
degraded environments retain the deterministic in-process limiter used by tests; a
Redis outage must not turn a verification endpoint into a hard 500.

THE FALLBACK USED TO BE SILENT, AND THAT WAS TWO PROBLEMS (board c292).

Operationally: if Redis becomes unreachable, every instance quietly starts keeping its
own private window. Nothing errors, nothing 500s, no line appears anywhere - the limits
just stop being shared, so the real ceiling silently multiplies by the instance count
and stays that way indefinitely. A degradation with no evidence is one nobody fixes.

Evidentially: a warning is positive evidence of local fallback, but its absence
cannot prove that Redis carried a request. Throttling, sink failure, process churn
and absent traffic all limit this sampled observation. A single-instance refusal
also cannot distinguish a local budget from a shared one; c290's cross-instance
enforcement claim requires independent evidence of the Redis path.

The warning is rate limited to at most one line per process per FALLBACK_WARNING_INTERVAL
- a Redis outage under load must not turn a degradation into a log flood, which would be
its own incident. It is deliberately not per-key: the point is "this process is degraded",
which is one fact, not one fact per user.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict, deque

from app.core.operational_signals import observe

logger = logging.getLogger(__name__)

# key -> monotonic timestamps of allowed calls within the current local window.
_WINDOWS: dict[str, deque[float]] = defaultdict(deque)

# At most one fallback warning per process per this many seconds. Ten minutes matches
# the shortest limiter window in use, so a persistent outage still leaves a steady
# heartbeat in the logs rather than one line at the start that scrolls away.
FALLBACK_WARNING_INTERVAL_SECONDS = 600.0
# Monotonic timestamp of the last warning, or None if none has been emitted.
_last_fallback_warning_at: float | None = None
# Each failure invalidates all successes from calls already in flight. An object
# token avoids retaining requests, keys or identities; no per-key state is added.
_fallback_epoch: object | None = None


def _warn_fallback_once(key: str, exc: BaseException) -> None:
    """Record that this process fell back to the in-process window. Rate limited.

    LOGS THE EXCEPTION TYPE, NEVER str(exc), and never the full key. Both rules are
    load-bearing rather than tidiness:

      - a redis-py connection error's str() can carry the connection URL, and REDIS_URL
        is a Secret Manager value. The same rule email_service already follows for the
        same reason.
      - keys are composed by callers as "<scope>:<subject>", and the subject is a user
        id, a client IP, or a student's email address (campus_verify_target). The scope
        alone answers "which limiter degraded"; the subject would put PII in the logs to
        answer nothing.
    """
    global _last_fallback_warning_at
    now = time.monotonic()
    if (
        _last_fallback_warning_at is not None
        and now - _last_fallback_warning_at < FALLBACK_WARNING_INTERVAL_SECONDS
    ):
        return
    _last_fallback_warning_at = now
    logger.warning(
        "rate limiter fell back to the in-process window scope=%s error=%s "
        "(limits are no longer shared across instances)",
        key.split(":", 1)[0],
        type(exc).__name__,
    )


def _allow_local(key: str, *, max_calls: int, window_seconds: float) -> bool:
    """Return True (and record a call) in the local fallback window."""
    now = time.monotonic()
    window = _WINDOWS[key]
    cutoff = now - window_seconds
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= max_calls:
        return False
    window.append(now)
    return True


async def allow(key: str, *, max_calls: int, window_seconds: float) -> bool:
    """Return True (and record a call) if `key` is under `max_calls`.

    Production uses a Redis fixed-window counter keyed on an arbitrary string (callers
    compose the key, e.g. ``prekey_bundle:{caller_id}:{target_id}``). Fixed windows are
    sufficient for this abuse-control layer and make the increment atomic across Cloud
    Run instances. Local mode and Redis failures use the in-process sliding window.

    The Redis timeout is deliberately short: rate limiting should never hold an API
    request open while Redis is unavailable. The local fallback remains a mitigation,
    not a hard cross-instance guarantee during an outage.
    """
    global _fallback_epoch
    # Avoid a network round-trip for local development and the test suite.
    from app.config import get_settings

    if get_settings().env == "local":
        return _allow_local(key, max_calls=max_calls, window_seconds=window_seconds)

    bucket = int(time.time() // window_seconds)
    redis_key = f"chirp:ratelimit:{key}:{bucket}"
    timeout = 0.5
    started_epoch = _fallback_epoch
    try:
        from app.ws.pubsub import get_redis

        redis = get_redis()
        count = int(await asyncio.wait_for(redis.incr(redis_key), timeout=timeout))
        if count == 1:
            # Best effort: the bucket suffix prevents a missing expiry from affecting
            # future windows, while expiry bounds normal Redis memory use.
            await asyncio.wait_for(
                redis.expire(redis_key, max(1, math.ceil(window_seconds))),
                timeout=timeout,
            )
        if started_epoch is not None and started_epoch is _fallback_epoch:
            # Only this completed operation is known to have used Redis after
            # the latest observed fallback. This is not fleet-wide recovery,
            # and it cannot predict a still-pending operation's later failure.
            _fallback_epoch = None
            observe("rate_limit_redis_success_after_fallback")
        return count <= max_calls
    except Exception as exc:
        _fallback_epoch = object()
        observe("rate_limit_fallback")
        try:
            _warn_fallback_once(key, exc)
        except Exception:
            # The legacy prose warning is also best-effort. A failed logger
            # cannot bypass the existing local abuse budget or leak diagnostics.
            pass
        return _allow_local(key, max_calls=max_calls, window_seconds=window_seconds)


def _reset_all() -> None:
    """Test-only: clear every tracked local window so tests do not bleed into each other."""
    global _last_fallback_warning_at, _fallback_epoch
    _WINDOWS.clear()
    # The warning suppressor is process-global too, so it bleeds the same way: without
    # this, the first test to trip the fallback would silence every later one.
    _last_fallback_warning_at = None
    _fallback_epoch = None
    from app.core.operational_signals import _reset_for_tests
    _reset_for_tests()
