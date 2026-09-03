"""c285 harness self-audit: ramp, abort grace, and the reference-probe verdict.

Each test was falsified before being kept (mutation named in its docstring).
"""
from __future__ import annotations

import pytest

from loadtest.abort import AbortMonitor
from loadtest.config import AbortCriteria, ConfigError
from loadtest.metrics import REFERENCE_CLASS, Recorder, Sample, instrument_verdict
from loadtest.runner import ramp_delay
from tests.test_c226_load_harness import _config_dict, _criteria, _fill, _write


# ---- ramp ----


def test_ramp_delay_spreads_users_evenly_and_degenerates_to_zero() -> None:
    """First user starts now, last at ramp_seconds, spacing even; one user or no
    ramp collapses to the pre-c285 all-at-once behavior.

    Falsified by: dividing by total instead of total - 1 (the last user started
    at 8.9s of a 10s ramp and the equality assertion failed)."""
    assert ramp_delay(0, 10, 10.0) == 0.0
    assert ramp_delay(9, 10, 10.0) == 10.0
    assert ramp_delay(5, 10, 10.0) == pytest.approx(50.0 / 9)
    assert ramp_delay(0, 1, 10.0) == 0.0
    assert ramp_delay(7, 10, 0.0) == 0.0


# ---- grace ----


def test_grace_holds_criteria_then_releases_them() -> None:
    """A violation-grade window is ignored before grace_seconds and fires at the
    first check after - both ways.

    Falsified by: deleting the grace early-return from AbortMonitor.check (the
    pre-grace check reported the violation)."""
    criteria = _criteria(grace_seconds=15.0, read_p95_ceiling_ms=100.0)
    recorder = Recorder(criteria.window_seconds)
    _fill(recorder, 100, 200, latency_ms=5000.0)
    monitor = AbortMonitor(criteria, recorder)
    assert monitor.check(10.0) == []  # inside grace: held
    violations = monitor.check(15.0)  # grace over: fires
    assert [v.criterion for v in violations] == ["read_p95_ms"]


def test_config_refuses_missing_grace_and_bad_ramp(tmp_path) -> None:
    """grace_seconds is a required abort criterion like every other; a ramp at
    least as long as the run is refused.

    Falsified by: letting load_config's require() default grace_seconds to 1
    (the missing-field case loaded fine)."""
    data = _config_dict()
    del data["abort"]["grace_seconds"]
    with pytest.raises(ConfigError):
        __import__("loadtest.config", fromlist=["load_config"]).load_config(_write(tmp_path, data))
    data = _config_dict()
    data["ramp_in_seconds"] = data["duration_seconds"]
    with pytest.raises(ConfigError):
        __import__("loadtest.config", fromlist=["load_config"]).load_config(_write(tmp_path, data))


def test_negative_grace_is_refused() -> None:
    """Falsified by: dropping the grace_seconds >= 0 check in AbortCriteria.validate."""
    with pytest.raises(ConfigError):
        _criteria(grace_seconds=-1.0).validate()


# ---- reference probe ----


def test_instrument_verdict_three_states() -> None:
    """saturated above the ratio, clean below it, and no_probe when the probe
    never ran - a missing probe must never read as a clean instrument.

    Falsified by: returning 'clean' from the probe_p95 <= 0 branch."""
    assert instrument_verdict(500.0, 100.0)["verdict"] == "saturated"
    assert instrument_verdict(500.0, 100.0)["ratio"] == 5.0
    assert instrument_verdict(250.0, 100.0)["verdict"] == "clean"
    assert instrument_verdict(500.0, 0.0)["verdict"] == "no_probe"


def test_probe_samples_never_dilute_the_abort_read_p95() -> None:
    """A fast probe must not drag the abort criteria's read p95 down below a
    ceiling the mix is genuinely violating.

    Falsified by: removing the REFERENCE_CLASS exclusion from
    Recorder.window_stats (49 fast probe rows pulled the p95 under the ceiling
    and the violation vanished)."""
    criteria = _criteria(read_p95_ceiling_ms=1000.0, grace_seconds=0.0, min_samples=10)
    recorder = Recorder(criteria.window_seconds)
    _fill(recorder, 51, 200, latency_ms=2000.0)  # the mix: violating
    for i in range(49):
        recorder.record(
            Sample(at=float(i) * 0.01, route_class=REFERENCE_CLASS, status=200, latency_ms=10.0)
        )
    violations = AbortMonitor(criteria, recorder).check(1.0)
    assert [v.criterion for v in violations] == ["read_p95_ms"]


def test_summary_reports_the_instrument_section() -> None:
    """The report carries the verdict computed from mix reads vs probe.

    Falsified by: computing the summary ratio from ALL reads including the probe
    (ratio fell below the saturation line and the verdict flipped to clean)."""
    recorder = Recorder(30.0)
    _fill(recorder, 100, 200, latency_ms=400.0)
    for i in range(100):
        recorder.record(
            Sample(at=float(i) * 0.01, route_class=REFERENCE_CLASS, status=200, latency_ms=100.0)
        )
    instrument = recorder.summary()["instrument"]
    assert instrument["verdict"] == "saturated"
    assert instrument["ratio"] == 4.0
    assert instrument["probe_p95_ms"] == 100.0
