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


def _reset_for_tests() -> None:
    with _lock:
        _last_emitted.clear()
