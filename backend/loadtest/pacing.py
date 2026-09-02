"""Token buckets and concurrency bounds: the harness's own hard rate caps."""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Classic token bucket; refuses (returns False) rather than queueing.

    Refusal over queueing is deliberate for the WRITE buckets: a queued write
    would burst the moment tokens refill, and a burst is exactly what the caps
    exist to prevent. The runner substitutes a read on refusal instead.
    """

    def __init__(self, rate_per_second: float, burst: float, *, clock=time.monotonic) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must allow at least one token")
        self._rate = rate_per_second
        self._burst = burst
        self._tokens = burst
        self._clock = clock
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self) -> None:
        """Wait for a token (used by the GLOBAL bucket, where waiting is the
        correct backpressure: it slows the whole run down instead of dropping)."""
        while not self.try_acquire():
            self._refill()
            deficit = 1 - self._tokens
            await asyncio.sleep(max(deficit / self._rate, 0.005))


class Pacer:
    """All pacing state for a run: one global bucket, one semaphore, and a
    per-(user, scope) bucket lattice for writes."""

    def __init__(
        self,
        max_rps: float,
        max_concurrent: int,
        per_user_writes_per_minute: dict[str, float],
        *,
        clock=time.monotonic,
    ) -> None:
        self.global_bucket = TokenBucket(max_rps, burst=max(2.0, max_rps), clock=clock)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._per_minute = per_user_writes_per_minute
        self._clock = clock
        self._user_buckets: dict[tuple[str, str], TokenBucket] = {}

    def write_allowed(self, uid: str, scope: str) -> bool:
        """True (and a token spent) if this user may perform this write now."""
        key = (uid, scope)
        bucket = self._user_buckets.get(key)
        if bucket is None:
            per_minute = self._per_minute[scope]
            # burst of 1: a virtual user never fires two capped writes
            # back-to-back, whatever the think-time dice do.
            bucket = TokenBucket(per_minute / 60.0, burst=1.0, clock=self._clock)
            self._user_buckets[key] = bucket
        return bucket.try_acquire()
