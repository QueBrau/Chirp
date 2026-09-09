"""Metrics recording: per-class latencies, status counts, rolling windows, timeline."""
from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Callable

# Route classes whose requests are writes; everything else counts as a read for
# the split p95 abort criteria. Kept here so the abort monitor and the report
# agree on the classification by construction.
WRITE_CLASSES = frozenset({"post_create", "comment_create", "chirp_create"})

# The self-audit probe (c285): one dedicated low-rate requester that bypasses
# every cap and shares no connection with the mix. Its latency is a consistency
# reference, not attribution of server time or proof of driver saturation.
# Excluded from the abort criteria's read set so the
# probe can never mask or dilute a real violation.
REFERENCE_CLASS = "reference_probe"

# A ratio over this value flags inconsistent latency. The legacy verdict names
# remain stable; neither side of the threshold establishes the cause.
SATURATION_RATIO = 3.0

TIMELINE_BUCKET_SECONDS = 10.0

WS_FAILURE_OUTCOMES = frozenset({
    "handshake_error", "handshake_timeout", "ready_timeout", "invalid_ready",
    "closed_before_ready", "closed_during_hold", "cleanup_timeout", "internal_error",
})
WS_OUTCOMES = WS_FAILURE_OUTCOMES | {"completed_hold", "stopped", "cancelled"}


def quantile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank quantile on an already-sorted list; 0.0 on empty input.

    Nearest-rank (ceil(q*n)) rather than interpolation: for an abort criterion a
    conservative, simple definition beats a fancier one nobody can re-derive at
    3am during a quiet-hour run.
    """
    if not sorted_values:
        return 0.0
    if not 0 < q <= 1:
        raise ValueError(f"quantile q must be in (0, 1], got {q}")
    rank = math.ceil(q * len(sorted_values))
    return sorted_values[rank - 1]


def instrument_verdict(mix_read_p95: float, probe_p95: float) -> dict:
    """The self-audit (c285): compare the mix's read p95 against the probe's.

    A ratio above SATURATION_RATIO flags a discrepancy for investigation.
    Different endpoint costs or driver contention can both contribute.
    Verdict states: 'saturated', 'clean', or 'no_probe' when the probe never ran
    (0.0 p95) - a missing probe must never read as a clean instrument.
    """
    if probe_p95 <= 0:
        return {"verdict": "no_probe", "ratio": None, "probe_p95_ms": 0.0}
    ratio = mix_read_p95 / probe_p95
    return {
        "verdict": "saturated" if ratio > SATURATION_RATIO else "clean",
        "ratio": round(ratio, 2),
        "probe_p95_ms": round(probe_p95, 1),
    }


@dataclass(frozen=True)
class Sample:
    at: float  # monotonic-ish seconds since run start
    route_class: str
    status: int  # HTTP status; 0 for transport error/timeout
    latency_ms: float


class Recorder:
    """Accumulates samples; thread-safe because the WS leg records from tasks too.

    Rolling-window state (for aborts) and whole-run state (for the report) are
    kept together so one record() call cannot update one and miss the other.
    """

    def __init__(self, window_seconds: float) -> None:
        self._lock = threading.Lock()
        self._window_seconds = window_seconds
        self._window: deque[Sample] = deque()
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._statuses: dict[str, Counter] = defaultdict(Counter)
        self._timeline: dict[int, Counter] = defaultdict(Counter)
        self._substituted_writes = 0
        self.ws_attempts = 0
        self.ws_connected = 0
        self.ws_close_codes: Counter = Counter()
        self.ws_connect_ms: list[float] = []
        self.ws_ready_ms: list[float] = []
        self.ws_outcomes: Counter = Counter()
        self.ws_cleanup_timeouts = 0
        self._ready: dict[int, Callable[[], bool]] = {}
        self._http_mix_active = False
        self._mixed_responses = 0
        self._mixed_successes = 0

    # ---- HTTP ----

    def record(self, sample: Sample) -> None:
        with self._lock:
            if (self._http_mix_active and sample.route_class != REFERENCE_CLASS
                    and sample.status >= 100 and any(is_open() for is_open in self._ready.values())):
                self._mixed_responses += 1
                self._mixed_successes += int(200 <= sample.status < 300)
            self._window.append(sample)
            self._trim(sample.at)
            self._latencies[sample.route_class].append(sample.latency_ms)
            self._statuses[sample.route_class][sample.status] += 1
            bucket = int(sample.at // TIMELINE_BUCKET_SECONDS)
            self._timeline[bucket]["requests"] += 1
            if sample.status == 429:
                self._timeline[bucket]["s429"] += 1
            elif sample.status == 0 or sample.status >= 500:
                self._timeline[bucket]["errors"] += 1

    def record_substituted_write(self) -> None:
        """A write was picked by the mix but its pacing bucket was empty, so a
        read ran instead — counted so the report can say how often pacing bit."""
        with self._lock:
            self._substituted_writes += 1

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._window and self._window[0].at < cutoff:
            self._window.popleft()

    # ---- WS ----

    def set_http_mix_active(self, active: bool) -> None:
        with self._lock:
            self._http_mix_active = active

    def record_ws_ready(self, ready_ms: float, is_open: Callable[[], bool]) -> int:
        with self._lock:
            self.ws_ready_ms.append(ready_ms)
            key = len(self.ws_ready_ms)
            self._ready[key] = is_open
            return key

    def end_ws_ready(self, key: int) -> None:
        with self._lock:
            self._ready.pop(key, None)

    def record_ws_outcome(self, outcome: str, *, cleanup_timeout: bool = False) -> None:
        if outcome not in WS_OUTCOMES:
            raise ValueError("unknown WS outcome")
        with self._lock:
            self.ws_outcomes[outcome] += 1
            self.ws_cleanup_timeouts += int(cleanup_timeout)

    def ws_terminal_stats(self) -> tuple[int, float]:
        with self._lock:
            settled = sum(self.ws_outcomes[k] for k in WS_FAILURE_OUTCOMES | {"completed_hold"})
            return settled, self._ws_failure_pct_unlocked()

    def record_ws_attempt(self) -> None:
        with self._lock:
            self.ws_attempts += 1

    def record_ws_connected(self, connect_ms: float) -> None:
        with self._lock:
            self.ws_connected += 1
            self.ws_connect_ms.append(connect_ms)

    def record_ws_close(self, code: int) -> None:
        with self._lock:
            self.ws_close_codes[code] += 1

    # ---- Rolling views (abort monitor) ----

    def window_stats(self, now: float) -> dict[str, float]:
        """Error/429 rates and read/write p95 over the rolling window."""
        with self._lock:
            self._trim(now)
            samples = list(self._window)
        total = len(samples)
        if total == 0:
            return {
                "samples": 0.0,
                "error_rate_pct": 0.0,
                "rate_429_pct": 0.0,
                "read_p95_ms": 0.0,
                "write_p95_ms": 0.0,
            }
        errors = sum(1 for s in samples if s.status == 0 or s.status >= 500)
        s429 = sum(1 for s in samples if s.status == 429)
        reads = sorted(
            s.latency_ms
            for s in samples
            if s.route_class not in WRITE_CLASSES and s.route_class != REFERENCE_CLASS
        )
        writes = sorted(s.latency_ms for s in samples if s.route_class in WRITE_CLASSES)
        return {
            "samples": float(total),
            "error_rate_pct": 100.0 * errors / total,
            "rate_429_pct": 100.0 * s429 / total,
            "read_p95_ms": quantile(reads, 0.95),
            "write_p95_ms": quantile(writes, 0.95),
        }

    def ws_failure_pct(self) -> float:
        with self._lock:
            return self._ws_failure_pct_unlocked()

    def _ws_failure_pct_unlocked(self) -> float:
        # Callers already holding self._lock (summary) use this directly: the
        # lock is a plain threading.Lock, so re-taking it self-deadlocks — that
        # exact hang cost this harness its first proving run.
        failures = sum(self.ws_outcomes[k] for k in WS_FAILURE_OUTCOMES)
        denominator = failures + self.ws_outcomes["completed_hold"]
        if denominator == 0:
            return 0.0
        return 100.0 * failures / denominator

    # ---- Whole-run summary (report) ----

    def summary(self) -> dict:
        with self._lock:
            classes = {}
            for route_class, latencies in sorted(self._latencies.items()):
                ordered = sorted(latencies)
                statuses = self._statuses[route_class]
                classes[route_class] = {
                    "count": len(ordered),
                    "statuses": {str(k): v for k, v in sorted(statuses.items())},
                    "p50_ms": round(quantile(ordered, 0.50), 1),
                    "p95_ms": round(quantile(ordered, 0.95), 1),
                    "p99_ms": round(quantile(ordered, 0.99), 1),
                    "max_ms": round(max(ordered), 1) if ordered else 0.0,
                }
            timeline = [
                {
                    "t_start_s": bucket * TIMELINE_BUCKET_SECONDS,
                    "requests": counts["requests"],
                    "errors": counts["errors"],
                    "s429": counts["s429"],
                }
                for bucket, counts in sorted(self._timeline.items())
            ]
            ws_ordered = sorted(self.ws_connect_ms)
            ready_ordered = sorted(self.ws_ready_ms)
            settled = sum(self.ws_outcomes[k] for k in WS_FAILURE_OUTCOMES | {"completed_hold"})
            mix_reads = sorted(
                v
                for route_class, values in self._latencies.items()
                if route_class not in WRITE_CLASSES and route_class != REFERENCE_CLASS
                for v in values
            )
            probe = sorted(self._latencies.get(REFERENCE_CLASS, []))
            instrument = instrument_verdict(quantile(mix_reads, 0.95), quantile(probe, 0.95))
            return {
                "http": classes,
                "substituted_writes": self._substituted_writes,
                "instrument": instrument,
                "timeline": timeline,
                "mixed_observation": {
                    "http_responses_while_ready_ws_open": self._mixed_responses,
                    "http_2xx_while_ready_ws_open": self._mixed_successes,
                    "observed": self._mixed_responses > 0,
                },
                "ws": {
                    "attempts": self.ws_attempts,
                    "connected": self.ws_connected,
                    "ready": len(self.ws_ready_ms),
                    "active_ready": len(self._ready),
                    "pending": self.ws_attempts - sum(self.ws_outcomes.values()),
                    "settled_for_failure_rate": settled,
                    "failure_pct": round(self._ws_failure_pct_unlocked(), 2) if settled else None,
                    "outcomes": {k: self.ws_outcomes[k] for k in sorted(WS_OUTCOMES)},
                    "cleanup_timeouts": self.ws_cleanup_timeouts,
                    "close_codes": {str(k): v for k, v in sorted(self.ws_close_codes.items())},
                    "connect_p50_ms": round(quantile(ws_ordered, 0.50), 1),
                    "connect_p95_ms": round(quantile(ws_ordered, 0.95), 1),
                    "ready_p95_ms": round(quantile(ready_ordered, 0.95), 1) if ready_ordered else None,
                },
            }
