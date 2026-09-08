"""Offline arithmetic and real CLI boundaries; no billing, app or DB calls."""
from __future__ import annotations

import copy
from datetime import date
from decimal import localcontext
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("c371_spend", ROOT / "scripts/spend_report.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)
TODAY = date(2026, 9, 8)
SECRET = "private-input-must-never-be-echoed"
FAILURE = {"result": "REPORT_NOT_WRITTEN", "error": "invalid_input_or_output"}


@pytest.fixture
def policy() -> dict:
    return S.load(ROOT / "infra/spend-policy.json")


@pytest.fixture
def supplied() -> dict:
    return S.load(ROOT / "infra/examples/spend-synthetic.json")


def calculate(supplied: dict, policy: dict) -> dict:
    return S.report(supplied, policy, today=TODAY)


def cli(input_path: Path, output_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/spend_report.py"), "--input", str(input_path),
         "--report", str(output_path), *extra], capture_output=True, text=True, timeout=3,
    )


def assert_private_error(result: subprocess.CompletedProcess, output: Path) -> None:
    assert result.returncode == 2
    assert json.loads(result.stdout) == FAILURE
    assert result.stderr == ""
    assert SECRET not in result.stdout
    assert not output.exists()


def test_synthetic_components_forecast_and_allocations(supplied: dict, policy: dict) -> None:
    result = calculate(supplied, policy)
    assert result["totals"] == {"gross": "40.005", "credits": "-5.005", "adjustments": "1", "net": "36", "net_rounded_cents": "36.00"}
    assert result["review"]["comparison_basis"] == "supplied_period_net"
    assert result["review"]["difference_from_monthly_target"] == "-89"
    assert not any(t["crossed"] for t in result["review"]["thresholds"])
    forecast = result["linear_forecast"]
    assert forecast["net_rounded_cents"] == "159.43"
    assert forecast["review_threshold_crossed"]
    assert forecast["comparison_basis"] == "assumed_linear_monthly_net"
    assert forecast["projects_credits_and_adjustments_at_same_rate"]
    assert [d["usd_per_unit_rounded"] for d in result["allocations"]] == ["0.03600000", "1.80000000"]
    assert all(d["marginal_price"] is False for d in result["allocations"])
    assert result["coverage"] == "supplied_components_only"
    assert not result["provider_verified"] and not result["billing_completeness_verified"]
    assert not result["notifications_configured"] and not result["review"]["monthly_compliance_proven"]


def test_no_implicit_forecast_or_unit_prices(supplied: dict, policy: dict) -> None:
    supplied.pop("forecast")
    supplied.pop("denominators")
    supplied.pop("adjustments")
    result = calculate(supplied, policy)
    assert result["linear_forecast"] is None and result["allocations"] == []
    assert result["totals"]["net"] == "35"


@pytest.mark.parametrize("gross,expected", [("124.999999999", False), ("125", True), ("125.000000001", True)])
def test_thresholds_use_unrounded_net(supplied: dict, policy: dict, gross: str, expected: bool) -> None:
    supplied["amounts"] = [{"category": "other", "gross": gross, "credits": "0"}]
    supplied["adjustments"] = "0"
    result = calculate(supplied, policy)
    assert result["totals"]["net_rounded_cents"] == "125.00"
    assert result["review"]["thresholds"][-1]["crossed"] is expected


def test_large_cancellation_and_tiny_adjustment_remain_exact(supplied: dict, policy: dict) -> None:
    supplied["amounts"] = [{"category": c, "gross": "999999999999.999999999", "credits": "-999999999999.999999998"} for c in sorted(S.CATEGORIES)]
    supplied["adjustments"] = "0.000000003"
    with localcontext() as context:
        context.prec = 8  # The caller's context must not corrupt the report.
        result = calculate(supplied, policy)
    assert result["totals"]["gross"] == "13999999999999.999999986"
    assert result["totals"]["credits"] == "-13999999999999.999999972"
    assert result["totals"]["net"] == "0.000000017"


def test_negative_adjustment_and_net_are_preserved(supplied: dict, policy: dict) -> None:
    supplied["adjustments"] = "-36.001"
    result = calculate(supplied, policy)
    assert result["totals"]["net"] == "-1.001"
    assert result["totals"]["net_rounded_cents"] == "-1.00"
    assert result["linear_forecast"]["net_rounded_cents"] == "-4.43"


@pytest.mark.parametrize("start,end,today,month_days,projected", [
    ("2024-02-01", "2024-02-02", date(2024, 2, 3), 29, "522.00"),
    ("2025-02-01", "2025-02-02", date(2025, 2, 3), 28, "504.00"),
    ("2026-04-01", "2026-04-30", date(2026, 5, 1), 30, "36.00"),
])
def test_calendar_forecast_uses_inclusive_completed_days(supplied: dict, policy: dict, start: str, end: str, today: date, month_days: int, projected: str) -> None:
    supplied["period"] = {"start": start, "end": end}
    result = S.report(supplied, policy, today=today)
    assert result["linear_forecast"]["month_days"] == month_days
    assert result["linear_forecast"]["net_rounded_cents"] == projected


@pytest.mark.parametrize("start,end", [
    ("2026-08-02", "2026-08-07"), ("2026-08-01", "2026-09-01"),
    ("2026-09-01", "2026-09-08"), ("2026-09-01", "2026-09-09"),
    ("2026-08-07", "2026-08-01"), ("2026-02-01", "2026-02-29"),
    ("2026-8-01", "2026-08-07"),
])
def test_partial_day_future_cross_month_and_invalid_dates_rejected(supplied: dict, policy: dict, start: str, end: str) -> None:
    supplied["period"] = {"start": start, "end": end}
    with pytest.raises(ValueError):
        calculate(supplied, policy)


def test_future_whole_month_estimate_is_labeled_and_cannot_be_forecast_again(supplied: dict, policy: dict) -> None:
    supplied["basis"] = "estimated"
    supplied["period"] = {"start": "2027-02-01", "end": "2027-02-28"}
    supplied.pop("forecast")
    result = calculate(supplied, policy)
    assert result["data_basis"] == "supplied_estimated"
    assert result["review"]["comparison_basis"] == "supplied_month_estimate_net"
    assert result["linear_forecast"] is None
    supplied["forecast"] = {"method": "linear_calendar_days", "assumes_complete_covered_days": True, "assumes_daily_net_rate_continues": True}
    with pytest.raises(ValueError):
        calculate(supplied, policy)
    supplied.pop("forecast")
    supplied["period"]["end"] = "2027-02-27"
    with pytest.raises(ValueError):
        calculate(supplied, policy)


@pytest.mark.parametrize("field,value", [("assumes_complete_covered_days", False), ("assumes_daily_net_rate_continues", 1), ("method", "monthly_average")])
def test_forecast_requires_explicit_exact_assumptions(supplied: dict, policy: dict, field: str, value: object) -> None:
    supplied["forecast"][field] = value
    with pytest.raises(ValueError):
        calculate(supplied, policy)


@pytest.mark.parametrize("update,reason", [
    ({"period": {"start": "2026-08-01", "end": "2026-08-06"}}, "period_mismatch"),
    ({"unit": "bytes"}, "unit_mismatch"),
    ({"value": "0"}, "nonpositive_or_fractional_count"),
    ({"value": "-1"}, "nonpositive_or_fractional_count"),
    ({"value": "1.5"}, "nonpositive_or_fractional_count"),
])
def test_bad_denominator_never_fabricates_unit_cost(supplied: dict, policy: dict, update: dict, reason: str) -> None:
    supplied["denominators"][0].update(update)
    result = calculate(supplied, policy)
    allocation = result["allocations"][0]
    assert allocation == {"metric": "accepted_writes", "unit": "count", "available": False, "reason": reason}
    assert result["allocations"][1]["available"]  # Independent valid denominator survives.


def test_tiny_positive_socket_hours_and_exact_large_count(supplied: dict, policy: dict) -> None:
    supplied["denominators"][0]["value"] = "999999999999999999"
    supplied["denominators"][1]["value"] = "0.000000001"
    result = calculate(supplied, policy)
    assert result["allocations"][0]["value"] == "999999999999999999"
    assert result["allocations"][0]["usd_per_unit_rounded"] == "0.00000000"
    assert result["allocations"][1]["usd_per_unit_rounded"] == "36000000000.00000000"


@pytest.mark.parametrize("value", [1.2, True, "NaN", "Infinity", "1e3", "+1", "01", "1.0000000001", "1000000000000", SECRET])
def test_invalid_money_rejected(supplied: dict, policy: dict, value: object) -> None:
    supplied["amounts"][0]["gross"] = value
    with pytest.raises(ValueError):
        calculate(supplied, policy)


@pytest.mark.parametrize("change", ["positive_credit", "negative_gross", "duplicate_category", "unknown_category", "unknown_key", "extra_denominator", "unknown_metric", "bool_version", "non_usd"])
def test_no_ambiguous_signs_duplicates_or_undeclared_schema(supplied: dict, policy: dict, change: str) -> None:
    if change == "positive_credit": supplied["amounts"][0]["credits"] = "1"
    elif change == "negative_gross": supplied["amounts"][0]["gross"] = "-1"
    elif change == "duplicate_category": supplied["amounts"].append(copy.deepcopy(supplied["amounts"][0]))
    elif change == "unknown_category": supplied["amounts"][0]["category"] = SECRET
    elif change == "unknown_key": supplied["project_identifier"] = SECRET
    elif change == "extra_denominator": supplied["denominators"].append(copy.deepcopy(supplied["denominators"][0]))
    elif change == "unknown_metric": supplied["denominators"][0]["metric"] = "DAU"
    elif change == "bool_version": supplied["version"] = True
    elif change == "non_usd": supplied["currency"] = "EUR"
    with pytest.raises(ValueError):
        calculate(supplied, policy)


@pytest.mark.parametrize("field,value", [("monthly_planning_target", "0"), ("currency", "EUR"), ("review_thresholds", ["0.8", "0.5"]), ("review_thresholds", ["1", "1.0"]), ("notifications_configured", True)])
def test_policy_cannot_claim_alerts_or_invalid_target(supplied: dict, policy: dict, field: str, value: object) -> None:
    policy[field] = value
    with pytest.raises(ValueError):
        calculate(supplied, policy)


@pytest.mark.parametrize("content", [
    '{"basis":"reported","basis":"estimated"}',
    '{"private":"' + SECRET + '","nested":' + '[' * 10 + '0' + ']' * 10 + '}',
    '{"value":NaN}', '{"value":Infinity}', '{',
    '{"private":"' + SECRET + '"' + ' ' * S.MAX_BYTES + '}',
    '{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}',
])
def test_cli_rejects_bad_json_without_echo_or_report(tmp_path: Path, content: str) -> None:
    source, output = tmp_path / "input.json", tmp_path / "report.json"
    source.write_text(content)
    assert_private_error(cli(source, output), output)


def test_real_cli_private_new_output_and_content_free_status(tmp_path: Path) -> None:
    output = tmp_path / "new.json"
    previous = os.umask(0o777)
    try:
        result = cli(ROOT / "infra/examples/spend-synthetic.json", output)
    finally:
        os.umask(previous)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {"result": "REPORT_WRITTEN", "provider_verified": False, "notifications_configured": False}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["totals"]["net"] == "36"
    assert "36" not in result.stdout  # Costs only enter the private artifact.


def test_cli_refuses_existing_output_and_symlink_without_touching_target(tmp_path: Path) -> None:
    source = ROOT / "infra/examples/spend-synthetic.json"
    target, link = tmp_path / "existing.json", tmp_path / "link.json"
    target.write_text(SECRET)
    link.symlink_to(target)
    for output in (target, link):
        result = cli(source, output)
        assert result.returncode == 2 and json.loads(result.stdout) == FAILURE and result.stderr == ""
        assert target.read_text() == SECRET
    assert link.is_symlink()


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "missing"])
def test_nonregular_input_cannot_block_or_get_followed(tmp_path: Path, kind: str) -> None:
    source, output = tmp_path / "input", tmp_path / "report.json"
    if kind == "fifo": os.mkfifo(source)
    elif kind == "directory": source.mkdir()
    elif kind == "symlink": source.symlink_to(ROOT / "infra/examples/spend-synthetic.json")
    assert_private_error(cli(source, output), output)


def test_invalid_arguments_and_policy_do_not_echo_private_values(tmp_path: Path) -> None:
    source, output = ROOT / "infra/examples/spend-synthetic.json", tmp_path / "report.json"
    assert_private_error(cli(source, output, "--" + SECRET), output)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"private": SECRET}))
    assert_private_error(cli(source, output, "--policy", str(policy_path)), output)


@pytest.mark.parametrize("replace_path", [False, True])
def test_failed_write_preserves_partial_file_and_never_unlinks_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, replace_path: bool) -> None:
    output = tmp_path / "report.json"
    real_fdopen = os.fdopen

    class FailedWriter:
        def __init__(self, descriptor: int) -> None:
            self.handle = real_fdopen(descriptor, "wb")

        def __enter__(self) -> FailedWriter:
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()

        def fileno(self) -> int:
            return self.handle.fileno()

        def write(self, payload: bytes) -> None:
            self.handle.write(payload[:10])
            self.handle.flush()
            assert stat.S_IMODE(output.stat().st_mode) == 0o600
            if replace_path:
                # A concurrent writer may replace the name while we retain our fd.
                replacement = tmp_path / "replacement.json"
                replacement.write_text(SECRET)
                replacement.replace(output)
            raise OSError(SECRET)

    def fdopen(descriptor: int, mode: str) -> object:
        return FailedWriter(descriptor) if mode == "wb" else real_fdopen(descriptor, mode)

    monkeypatch.setattr(S.os, "fdopen", fdopen)
    result = S.main(["--input", str(ROOT / "infra/examples/spend-synthetic.json"), "--report", str(output)])
    captured = capsys.readouterr()
    assert result == 2 and json.loads(captured.out) == FAILURE and captured.err == ""
    assert output.read_bytes() == (SECRET.encode() if replace_path else b'{\n  "versi')
