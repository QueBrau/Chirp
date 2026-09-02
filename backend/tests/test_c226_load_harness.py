"""c226 harness unit tests: pacing caps, abort criteria, and the park guard.

Pure client-side logic — no server, no database. Each test was falsified before
being kept (the mutation that turns it red is named in its docstring), per the
c245/c252/c259 house standard.
"""
from __future__ import annotations

import json
import threading

import pytest

from loadtest.abort import AbortMonitor
from loadtest.accounts import auth_headers, load_manifest
from loadtest.config import AbortCriteria, ConfigError, load_config
from loadtest.metrics import Recorder, Sample, quantile
from loadtest.pacing import Pacer, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---- quantiles ----


def test_quantile_nearest_rank() -> None:
    """Falsified by: off-by-one in the rank index (rank-2 instead of rank-1)."""
    values = [float(v) for v in range(1, 101)]
    assert quantile(values, 0.95) == 95.0
    assert quantile(values, 0.50) == 50.0
    assert quantile(values, 1.0) == 100.0
    assert quantile([], 0.95) == 0.0


def test_quantile_rejects_bad_q() -> None:
    """Falsified by: dropping the bounds check in quantile()."""
    with pytest.raises(ValueError):
        quantile([1.0], 0.0)
    with pytest.raises(ValueError):
        quantile([1.0], 1.5)


def test_summary_does_not_self_deadlock_with_ws_data() -> None:
    """Regression for the first proving run, which hung at report time: summary()
    held the recorder lock and called ws_failure_pct(), which re-takes the same
    non-reentrant lock. Falsified by: reverting summary() to the locked call."""
    recorder = Recorder(30.0)
    recorder.record_ws_attempt()
    recorder.record_ws_connected(12.0)
    result: list = []
    worker = threading.Thread(target=lambda: result.append(recorder.summary()), daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert result, "summary() deadlocked"
    assert result[0]["ws"]["attempts"] == 1


# ---- token bucket, both ways ----


def test_token_bucket_allows_within_burst_and_refuses_beyond() -> None:
    """Falsified by: removing the `self._tokens -= 1` spend in try_acquire."""
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=1.0, burst=2.0, clock=clock)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()  # burst exhausted, no time has passed


def test_token_bucket_refills_at_rate_not_faster() -> None:
    """Falsified by: dropping the min(burst, ...) clamp in _refill."""
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=2.0, burst=2.0, clock=clock)
    assert bucket.try_acquire() and bucket.try_acquire()
    clock.advance(0.4)  # 0.8 tokens: not enough
    assert not bucket.try_acquire()
    clock.advance(0.2)  # 1.2 tokens now
    assert bucket.try_acquire()
    # A long idle period must not bank more than burst.
    clock.advance(3600)
    assert bucket.try_acquire() and bucket.try_acquire()
    assert not bucket.try_acquire()


# ---- per-user write pacing, both ways ----


def test_pacer_write_cap_blocks_a_loop_and_passes_a_paced_user() -> None:
    """The c259-style both-ways proof for the harness's own caps: a back-to-back
    second write is refused (burst 1), a write after the per-minute interval
    passes. Falsified by: burst=1.0 -> 2.0 in Pacer.write_allowed."""
    clock = FakeClock()
    pacer = Pacer(10.0, 5, {"chirp_create": 1.5}, clock=clock)
    assert pacer.write_allowed("u1", "chirp_create")
    assert not pacer.write_allowed("u1", "chirp_create")  # immediate retry: refused
    clock.advance(60 / 1.5)  # one full interval at 1.5/min
    assert pacer.write_allowed("u1", "chirp_create")


def test_pacer_caps_are_per_user_not_global() -> None:
    """One exhausted user must not spend another user's budget (mirrors c259's
    per-user design). Falsified by: keying _user_buckets on scope alone."""
    clock = FakeClock()
    pacer = Pacer(10.0, 5, {"chirp_create": 1.5}, clock=clock)
    assert pacer.write_allowed("u1", "chirp_create")
    assert not pacer.write_allowed("u1", "chirp_create")
    assert pacer.write_allowed("u2", "chirp_create")


# ---- abort monitor, both ways ----


def _criteria(**overrides) -> AbortCriteria:
    base = dict(
        max_error_rate_pct=2.0,
        max_429_rate_pct=1.0,
        read_p95_ceiling_ms=800.0,
        write_p95_ceiling_ms=1500.0,
        max_ws_failure_pct=5.0,
        window_seconds=30.0,
        min_samples=10,
    )
    base.update(overrides)
    return AbortCriteria(**base)


def _fill(recorder: Recorder, count: int, status: int, route_class: str = "feed_campus", latency_ms: float = 50.0) -> None:
    for i in range(count):
        recorder.record(Sample(at=float(i) * 0.01, route_class=route_class, status=status, latency_ms=latency_ms))


def test_abort_trips_on_error_rate_and_not_on_clean_traffic() -> None:
    """Falsified by: deleting the error-rate check from AbortMonitor.check."""
    criteria = _criteria()
    clean = Recorder(criteria.window_seconds)
    _fill(clean, 100, 200)
    assert AbortMonitor(criteria, clean).check(1.0) == []

    dirty = Recorder(criteria.window_seconds)
    _fill(dirty, 95, 200)
    _fill(dirty, 5, 500)  # 5% > 2% ceiling
    violations = AbortMonitor(criteria, dirty).check(1.0)
    assert [v.criterion for v in violations] == ["error_rate_pct"]


def test_abort_counts_429_separately_from_errors() -> None:
    """A 429 storm must trip its own criterion, not the error one. Falsified by:
    classifying 429 as an error in Recorder.window_stats."""
    criteria = _criteria()
    recorder = Recorder(criteria.window_seconds)
    _fill(recorder, 95, 200)
    _fill(recorder, 5, 429)
    violations = AbortMonitor(criteria, recorder).check(1.0)
    assert [v.criterion for v in violations] == ["rate_429_pct"]


def test_abort_respects_min_samples() -> None:
    """One early failure must not abort a run. Falsified by: evaluating the
    window before the min_samples gate."""
    criteria = _criteria(min_samples=10)
    recorder = Recorder(criteria.window_seconds)
    _fill(recorder, 5, 500)  # 100% errors, but only 5 samples
    assert AbortMonitor(criteria, recorder).check(1.0) == []


def test_abort_trips_on_read_p95_ceiling() -> None:
    """Falsified by: comparing read p95 against the WRITE ceiling."""
    criteria = _criteria(read_p95_ceiling_ms=100.0)
    recorder = Recorder(criteria.window_seconds)
    _fill(recorder, 100, 200, latency_ms=250.0)
    violations = AbortMonitor(criteria, recorder).check(1.0)
    assert [v.criterion for v in violations] == ["read_p95_ms"]


# ---- config loading: the park guard and the cap ceiling ----


def _config_dict() -> dict:
    return {
        "base_url": "http://127.0.0.1:8010",
        "ws_url": "ws://127.0.0.1:8010/ws",
        "auth_mode": "emulated",
        "duration_seconds": 60,
        "think_seconds": 2.0,
        "mix_weights": {"feed_campus": 30, "chirp_create": 8},
        "caps": {
            "max_rps": 40,
            "max_concurrent_requests": 30,
            "per_user_writes_per_minute": {
                "post_create": 3.0,
                "comment_create": 3.0,
                "chirp_create": 1.5,
            },
        },
        "abort": {
            "max_error_rate_pct": 2.0,
            "max_429_rate_pct": 1.0,
            "read_p95_ceiling_ms": 800,
            "write_p95_ceiling_ms": 1500,
            "max_ws_failure_pct": 5.0,
            "window_seconds": 30,
            "min_samples": 50,
        },
        "ws": {"max_sockets": 40, "connects_per_second": 5, "hold_seconds": 15},
    }


def _write(tmp_path, data: dict) -> str:
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def test_config_valid_local_loads(tmp_path) -> None:
    """Falsified by: making _is_local() never match, so a good local config was refused."""
    config = load_config(_write(tmp_path, _config_dict()))
    assert config.base_url == "http://127.0.0.1:8010"


def test_config_refuses_missing_abort_criteria(tmp_path) -> None:
    """The c226 rule itself: no abort criteria, no run. Falsified by: letting
    load_config's require() fall back to a default instead of raising."""
    data = _config_dict()
    del data["abort"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))
    data = _config_dict()
    del data["abort"]["max_error_rate_pct"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_config_refuses_write_cap_above_half_the_c259_limit(tmp_path) -> None:
    """Falsified by: raising the 0.5 factor in RateCaps.validate to 1.0."""
    data = _config_dict()
    data["caps"]["per_user_writes_per_minute"]["chirp_create"] = 2.0  # >50% of 3/min
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_config_park_guard_blocks_non_local_target(tmp_path) -> None:
    """The park, both ways: refused without approval+flag, refused with emulated
    auth even WITH both, and allowed only with firebase + approval + flag.
    Falsified by: checking only base_url and not ws_url for localness."""
    data = _config_dict()
    data["base_url"] = "https://chirp-api-example.a.run.app"
    data["ws_url"] = "wss://chirp-ws-example.a.run.app/ws"
    data["auth_mode"] = "firebase"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))  # no approval block
    data["approval"] = {"approved_by": "jose", "date": "2026-09-01"}
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))  # approval but no flag
    config = load_config(_write(tmp_path, data), confirm_park_lifted=True)
    assert config.approved_by == "jose"

    data["auth_mode"] = "emulated"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data), confirm_park_lifted=True)

    # A non-local ws_url alone is still non-local.
    data = _config_dict()
    data["ws_url"] = "wss://chirp-ws-example.a.run.app/ws"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


# ---- manifest ----


def test_manifest_loads_and_validates(tmp_path) -> None:
    """Falsified by: dropping the firebase-mode id_token check in load_manifest."""
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {"campus_id": "c-1", "chapter_id": "ch-1", "users": [{"uid": "load-u0001"}]}
        )
    )
    manifest = load_manifest(str(path), auth_mode="emulated")
    assert manifest.users[0].uid == "load-u0001"
    assert auth_headers(manifest.users[0], "emulated") == {"X-Debug-Firebase-Uid": "load-u0001"}
    with pytest.raises(SystemExit):
        load_manifest(str(path), auth_mode="firebase")  # no id_token

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"campus_id": "c-1", "chapter_id": "ch-1", "users": []}))
    with pytest.raises(SystemExit):
        load_manifest(str(empty), auth_mode="emulated")
