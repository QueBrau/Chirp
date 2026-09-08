"""Offline arithmetic for supplied infrastructure costs; no billing verification or alerts."""
from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, AbstractSet

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 65536
CATEGORIES = frozenset({"api_compute", "ws_compute", "cloud_sql", "redis", "networking", "media_storage", "media_egress", "identity", "hosting", "email", "builds_artifacts", "logs_monitoring", "provider_fees", "other"})
UNITS = {"accepted_writes": "count", "egress_bytes": "bytes", "socket_hours": "hours"}


class InputError(ValueError):
    """All caller-visible errors are fixed labels, without supplied values."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InputError("invalid_arguments")


def keys(value: Any, required: set[str], optional: AbstractSet[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise InputError("invalid_shape")
    return value


def load(path: Path) -> dict:
    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise InputError("duplicate_key")
            result[key] = value
        return result

    def bounded(value: Any, depth: int = 0) -> None:
        if depth > 8:
            raise InputError("too_deep")
        if isinstance(value, dict):
            for nested in value.values():
                bounded(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value:
                bounded(nested, depth + 1)

    def invalid_constant(value: str) -> None:
        raise InputError("invalid_json_number")

    # A byte limit alone doesn't bound a FIFO open/read. Require a regular file
    # through the opened descriptor, with no final symlink or blocking FIFO open.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise InputError("regular_file_required")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise InputError("too_large")
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    bounded(value)
    if not isinstance(value, dict):
        raise InputError("invalid_shape")
    return value


def decimal(value: Any, *, count: bool = False) -> Decimal:
    # No floats, exponents, NaN or Infinity. At most 12 whole + 9 fractional
    # monetary digits; 14 categories + adjustments fit comfortably in precision50.
    whole_digits = 18 if count else 12
    pattern = rf"-?(?:0|[1-9][0-9]{{0,{whole_digits-1}}})(?:\.[0-9]{{1,9}})?"
    if not isinstance(value, str) or len(value) > whole_digits + 11 or not re.fullmatch(pattern, value):
        raise InputError("invalid_decimal")
    result = Decimal(value)
    if not result.is_finite():
        raise InputError("invalid_decimal")
    return result


def exact(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def rounded(value: Decimal, places: int = 2) -> str:
    result = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if result == 0:
        result = abs(result)
    return format(result, "f")


def dates(value: Any) -> tuple[date, date]:
    keys(value, {"start", "end"})
    parsed = []
    for key in ("start", "end"):
        text = value[key]
        if not isinstance(text, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            raise InputError("invalid_date")
        parsed.append(date.fromisoformat(text))
    if parsed[1] < parsed[0]:
        raise InputError("invalid_period")
    return parsed[0], parsed[1]


def policy_values(policy: dict) -> tuple[Decimal, list[Decimal], Decimal]:
    keys(policy, {"version", "currency", "monthly_planning_target", "review_thresholds", "forecast_review_threshold", "comparison_basis", "notifications_configured"})
    if type(policy["version"]) is not int or policy["version"] != 1 or policy["currency"] != "USD" or policy["comparison_basis"] != "supplied_net" or policy["notifications_configured"] is not False:
        raise InputError("invalid_policy")
    target = decimal(policy["monthly_planning_target"])
    thresholds = policy["review_thresholds"]
    if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 10:
        raise InputError("invalid_policy")
    thresholds = [decimal(t) for t in thresholds]
    forecast_threshold = decimal(policy["forecast_review_threshold"])
    if target <= 0 or thresholds != sorted(set(thresholds)) or any(t <= 0 or t > 10 for t in thresholds + [forecast_threshold]):
        raise InputError("invalid_policy")
    return target, thresholds, forecast_threshold


def report(data: dict, policy: dict, *, today: date | None = None) -> dict:
    """Calculate supplied subtotals with explicit calendar/denominator boundaries."""
    with localcontext() as context:
        context.prec = 50
        target, thresholds, forecast_threshold = policy_values(policy)
        keys(data, {"version", "basis", "currency", "period", "amounts"}, {"adjustments", "forecast", "denominators"})
        if type(data["version"]) is not int or data["version"] != 1 or data["basis"] not in ("reported", "estimated") or data["currency"] != "USD":
            raise InputError("invalid_basis_or_currency")
        start, end = dates(data["period"])
        month_days = calendar.monthrange(start.year, start.month)[1]
        if start.day != 1 or (start.year, start.month) != (end.year, end.month):
            raise InputError("monthly_coverage_required")
        current = today or datetime.now(timezone.utc).date()
        if data["basis"] == "reported" and end >= current:
            raise InputError("completed_dates_required")
        if data["basis"] == "estimated" and end.day != month_days:
            raise InputError("whole_month_estimate_required")
        entries = data["amounts"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= len(CATEGORIES):
            raise InputError("invalid_amounts")
        gross, credits = Decimal(0), Decimal(0)
        categories = set()
        components = []
        for entry in entries:
            keys(entry, {"category", "gross", "credits"})
            category = entry["category"]
            if not isinstance(category, str) or category not in CATEGORIES or category in categories:
                raise InputError("invalid_category")
            charge, credit = decimal(entry["gross"]), decimal(entry["credits"])
            if charge < 0 or credit > 0:
                raise InputError("invalid_cost_sign")
            categories.add(category)
            gross += charge
            credits += credit
            components.append({"category": category, "gross": exact(charge), "credits": exact(credit)})
        adjustments = decimal(data.get("adjustments", "0"))
        net = gross + credits + adjustments
        basis = "supplied_period_net" if data["basis"] == "reported" else "supplied_month_estimate_net"
        result = {
            "version": 1, "currency": "USD", "data_basis": "supplied_" + data["basis"],
            "provider_verified": False, "billing_completeness_verified": False,
            "notifications_configured": False, "coverage": "supplied_components_only",
            "period": {"start": start.isoformat(), "end": end.isoformat(), "inclusive": True},
            "components": components,
            "totals": {"gross": exact(gross), "credits": exact(credits), "adjustments": exact(adjustments), "net": exact(net), "net_rounded_cents": rounded(net)},
            "planning_target": {"period": "calendar_month", "amount": rounded(target)},
            "review": {"comparison_basis": basis, "difference_from_monthly_target": exact(net - target),
                       "thresholds": [{"fraction": exact(t), "amount": exact(target*t), "crossed": net >= target*t} for t in thresholds],
                       "monthly_compliance_proven": False},
            "linear_forecast": None, "allocations": [],
        }
        if "forecast" in data:
            forecast = keys(data["forecast"], {"method", "assumes_complete_covered_days", "assumes_daily_net_rate_continues"})
            if data["basis"] != "reported" or forecast["method"] != "linear_calendar_days" or forecast["assumes_complete_covered_days"] is not True or forecast["assumes_daily_net_rate_continues"] is not True:
                raise InputError("invalid_forecast_assumptions")
            projected = net * month_days / end.day
            result["linear_forecast"] = {
                "comparison_basis": "assumed_linear_monthly_net", "covered_days": end.day, "month_days": month_days,
                "assumes_complete_covered_days": True, "assumes_daily_net_rate_continues": True,
                "projects_credits_and_adjustments_at_same_rate": True,
                "net_rounded_cents": rounded(projected), "review_fraction": exact(forecast_threshold),
                "review_threshold_crossed": projected >= target*forecast_threshold,
                "formula": "supplied_net * month_days / covered_days",
            }
        denominators = data.get("denominators", [])
        if not isinstance(denominators, list) or len(denominators) > len(UNITS):
            raise InputError("invalid_denominators")
        seen = set()
        for denominator in denominators:
            keys(denominator, {"metric", "unit", "value", "period"})
            metric = denominator["metric"]
            if not isinstance(metric, str) or metric not in UNITS or metric in seen:
                raise InputError("invalid_metric")
            seen.add(metric)
            value = decimal(denominator["value"], count=True)
            denom_start, denom_end = dates(denominator["period"])
            issue = None
            if (denom_start, denom_end) != (start, end):
                issue = "period_mismatch"
            elif denominator["unit"] != UNITS[metric]:
                issue = "unit_mismatch"
            elif value <= 0 or (metric != "socket_hours" and value != value.to_integral_value()):
                issue = "nonpositive_or_fractional_count"
            allocation = {"metric": metric, "unit": UNITS[metric], "available": issue is None}
            if issue:
                allocation["reason"] = issue
            else:
                allocation.update(value=exact(value), comparison_basis=basis, numerator_net=exact(net), usd_per_unit_rounded=rounded(net/value, 8), rounding="8_places_half_up", marginal_price=False)
            result["allocations"].append(allocation)
        return result


def write_private(path: Path, result: dict) -> None:
    """Create a new 0600 regular file; refuse existing paths and final symlinks."""
    payload = (json.dumps(result, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # An interrupted write may leave a partial private file. Do not unlink by
    # pathname on failure: another process could have replaced that directory entry.
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)


def main(argv: list[str] | None = None) -> int:
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("--input", type=Path, required=True)
        parser.add_argument("--report", type=Path, required=True)
        parser.add_argument("--policy", type=Path, default=ROOT / "infra/spend-policy.json")
        args = parser.parse_args(argv)
        result = report(load(args.input), load(args.policy))
        write_private(args.report, result)
        print('{"result":"REPORT_WRITTEN","provider_verified":false,"notifications_configured":false}')
        return 0
    except (InputError, OSError, ValueError, TypeError, KeyError, RecursionError, InvalidOperation):
        print('{"result":"REPORT_NOT_WRITTEN","error":"invalid_input_or_output"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
