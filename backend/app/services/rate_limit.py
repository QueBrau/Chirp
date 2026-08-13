"""In-process sliding-window rate limiter (SECURITY-REVIEW finding 9)."""
from __future__ import annotations

import time
from collections import defaultdict, deque

# key -> monotonic timestamps of allowed calls within the current window.
_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


def allow(key: str, *, max_calls: int, window_seconds: float) -> bool:
    """Return True (and record a call) if `key` is under `max_calls` in the trailing window.

    Sliding-window counter keyed on an arbitrary string (callers compose the key, e.g.
    "prekey_bundle:{caller_id}:{target_id}"). Old entries older than `window_seconds` are
    dropped lazily on each call, so memory only grows with distinct active keys.

    CAVEAT — this is a per-process, in-memory limiter: a first-layer mitigation, not a hard
    guarantee. Under Cloud Run (or any multi-instance deploy) each instance keeps its own
    counters and a restart clears them, so a determined attacker spread across instances/
    restarts can exceed the nominal limit. Production should replace this with a Redis-backed
    counter — `app.ws.pubsub` already holds a Redis client (`get_redis()`) that a real
    implementation would reuse (e.g. INCR + EXPIRE / a sorted-set sliding window).
    """
    now = time.monotonic()
    window = _WINDOWS[key]
    cutoff = now - window_seconds
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= max_calls:
        return False
    window.append(now)
    return True


def _reset_all() -> None:
    """Test-only: clear every tracked window so tests don't bleed into each other."""
    _WINDOWS.clear()
