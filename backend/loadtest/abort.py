"""Abort monitor: evaluates the rolling window against the pre-written criteria."""
from __future__ import annotations

from dataclasses import dataclass

from loadtest.config import AbortCriteria
from loadtest.metrics import Recorder


@dataclass(frozen=True)
class Violation:
    criterion: str
    observed: float
    limit: float

    def describe(self) -> str:
        return f"{self.criterion}: observed {self.observed:.1f} against limit {self.limit:.1f}"


class AbortMonitor:
    """Stateless check called on a short interval by the runner; the FIRST
    non-empty result stops the run. There is no override path on purpose."""

    def __init__(self, criteria: AbortCriteria, recorder: Recorder) -> None:
        self._criteria = criteria
        self._recorder = recorder

    def check(self, now: float) -> list[Violation]:
        stats = self._recorder.window_stats(now)
        violations: list[Violation] = []
        if stats["samples"] >= self._criteria.min_samples:
            if stats["error_rate_pct"] > self._criteria.max_error_rate_pct:
                violations.append(
                    Violation("error_rate_pct", stats["error_rate_pct"], self._criteria.max_error_rate_pct)
                )
            if stats["rate_429_pct"] > self._criteria.max_429_rate_pct:
                violations.append(
                    Violation("rate_429_pct", stats["rate_429_pct"], self._criteria.max_429_rate_pct)
                )
            if stats["read_p95_ms"] > self._criteria.read_p95_ceiling_ms:
                violations.append(
                    Violation("read_p95_ms", stats["read_p95_ms"], self._criteria.read_p95_ceiling_ms)
                )
            # A window can hold min_samples total yet zero writes; a zero write
            # p95 correctly stays under any positive ceiling, so no guard needed.
            if stats["write_p95_ms"] > self._criteria.write_p95_ceiling_ms:
                violations.append(
                    Violation("write_p95_ms", stats["write_p95_ms"], self._criteria.write_p95_ceiling_ms)
                )
        ws_attempts = self._recorder.ws_attempts
        if ws_attempts >= self._criteria.min_samples:
            ws_failure = self._recorder.ws_failure_pct()
            if ws_failure > self._criteria.max_ws_failure_pct:
                violations.append(
                    Violation("ws_failure_pct", ws_failure, self._criteria.max_ws_failure_pct)
                )
        return violations
