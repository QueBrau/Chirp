"""Small, bounded operational observations; never request contents or identities.

These are sampled evidence, not exact failure counters or global health state.
Each of three fixed event names emits at most once per interval per process.
Persistent failures emit again on the next failing call after the interval;
there is no background probe and no assertion of health when traffic stops.
"""
from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger("app.operational")
_INTERVALS = {
    "sql_pool_capacity_503": 60.0,
    "rate_limit_fallback": 600.0,
    "rate_limit_redis_success_after_fallback": 600.0,
    # board c356: the outbox sweep loop already throttles its own cadence at
    # settings.outbox_sweep_interval_s, so this is always-emit (0.0) rather than a
    # second, redundant interval gate here.
    "outbox_queue_age": 0.0,
    # board c350: GET /media/{token} denied a redirect on the redirect-time
    # entitlement re-check. Throttled like the other per-process health signals
    # above (not the "always emit" outbox case) - a feed render can fan out to
    # 20+ media GETs per viewer, so a burst of revocations must not become a log
    # flood.
    "media_access_revoked": 60.0,
}
_last_emitted: dict[str, float] = {}
_lock = threading.Lock()


def observe(event: str) -> None:
    """Emit only a known observation; logging failures cannot change HTTP behavior."""
    try:
        if type(event) is not str or event not in _INTERVALS:
            return
        now = time.monotonic()
        with _lock:
            previous = _last_emitted.get(event)
            if previous is not None and now - previous < _INTERVALS[event]:
                return
            _last_emitted[event] = now
        level = (logging.INFO if event == "rate_limit_redis_success_after_fallback"
                 else logging.WARNING)
        logger.log(level, json.dumps({
            "schema_version": 1,
            "signal_family": "chirp_operational",
            "event": event,
            "severity": logging.getLevelName(level),
            "observation_scope": "process",
            "sampled": True,
        }, separators=(",", ":")))
    except Exception:
        # Never include the failing handler, exception, or original record in a
        # diagnostic: they can contain transport credentials. No recursive log.
        return


def report_queue_age(pending: int, oldest_age_seconds: float) -> None:
    """Emit the outbox sweeper's queue depth -- counts only, never an id or a payload.

    A sibling to `observe()`, not a call through it: `observe(event)` takes no
    payload argument at all, and widening it to accept arbitrary kwargs would widen
    the exact attack surface it exists to close (see module docstring and
    tests/test_c370_operational_signals.py's closedness tests, which this function
    does not touch -- "outbox_queue_age" is the only new fixed vocabulary entry and
    every existing event name/behavior is unchanged).
    """
    try:
        now = time.monotonic()
        with _lock:
            previous = _last_emitted.get("outbox_queue_age")
            if previous is not None and now - previous < _INTERVALS["outbox_queue_age"]:
                return
            _last_emitted["outbox_queue_age"] = now
        logger.log(logging.INFO, json.dumps({
            "schema_version": 1,
            "signal_family": "chirp_operational",
            "event": "outbox_queue_age",
            "severity": logging.getLevelName(logging.INFO),
            "observation_scope": "process",
            "sampled": True,
            "pending": int(pending),
            "oldest_age_seconds": round(float(oldest_age_seconds), 3),
        }, separators=(",", ":")))
    except Exception:
        # Same rule as observe(): never let a logging failure carry the original
        # value or an exception into a recursive log.
        return


def _reset_for_tests() -> None:
    with _lock:
        _last_emitted.clear()
