"""Small distributed rate limiter with a local fallback.

Production uses Redis fixed windows so Cloud Run instances share a budget. Local and
degraded environments retain the deterministic in-process limiter used by tests; a
Redis outage must not turn a verification endpoint into a hard 500.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque

# key -> monotonic timestamps of allowed calls within the current local window.
_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


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
    # Avoid a network round-trip for local development and the test suite.
    from app.config import get_settings

    if get_settings().env == "local":
        return _allow_local(key, max_calls=max_calls, window_seconds=window_seconds)

    bucket = int(time.time() // window_seconds)
    redis_key = f"chirp:ratelimit:{key}:{bucket}"
    timeout = 0.5
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
        return count <= max_calls
    except Exception:
        return _allow_local(key, max_calls=max_calls, window_seconds=window_seconds)


def _reset_all() -> None:
    """Test-only: clear every tracked local window so tests do not bleed into each other."""
    _WINDOWS.clear()
