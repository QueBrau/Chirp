"""Report writer: one JSON artifact plus a human-readable summary on stdout."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from loadtest.abort import Violation
from loadtest.config import HarnessConfig
from loadtest.metrics import Recorder
from loadtest.ws_leg import CLOSE_CODE_NAMES


def build_report(
    config: HarnessConfig,
    recorder: Recorder,
    violations: list[Violation],
    *,
    phases_run: list[str],
    wall_seconds: float,
    run_status: str = "completed",
) -> dict:
    summary = recorder.summary()
    ws = summary["ws"]
    if run_status == "completed" and (
        ws["pending"] or ws["outcomes"]["stopped"] or ws["outcomes"]["cancelled"]
    ):
        run_status = "incomplete"
    ws["close_codes_named"] = {
        CLOSE_CODE_NAMES.get(int(code), f"code_{code}"): count
        for code, count in ws["close_codes"].items()
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {"base_url": config.base_url, "ws_url": config.ws_url, "auth_mode": config.auth_mode},
        "phases_run": phases_run,
        "wall_seconds": round(wall_seconds, 1),
        "aborted": bool(violations),
        "run_status": "aborted" if violations and run_status not in {"internal_error", "interrupted"} else run_status,
        "coverage": {
            "http_ready_ws_overlap": "OBSERVED" if summary["mixed_observation"]["observed"] else "NOT_OBSERVED",
            "receipt_delivery": "NOT_PROVEN",
            "production_capacity": "NOT_PROVEN",
        },
        "abort_violations": [asdict(v) for v in violations],
        "config": {
            "duration_seconds": config.duration_seconds,
            "mix_weights": config.mix_weights,
            "think_seconds": config.think_seconds,
            "caps": asdict(config.caps),
            "abort": asdict(config.abort),
            "ws": asdict(config.ws),
        },
        "results": summary,
    }


def write_report(report: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


def print_summary(report: dict) -> None:
    print(f"target: {report['target']['base_url']} (auth {report['target']['auth_mode']})")
    print(f"phases: {', '.join(report['phases_run'])}; wall {report['wall_seconds']}s")
    if report["run_status"] in {"internal_error", "interrupted", "incomplete"}:
        print(f"RESULT: {report['run_status'].upper()} - incomplete run")
    elif report["aborted"]:
        print("RESULT: ABORTED by criteria:")
        for v in report["abort_violations"]:
            print(f"  {v['criterion']}: observed {v['observed']:.1f}, limit {v['limit']:.1f}")
    else:
        print("RESULT: completed without tripping any abort criterion")
    http = report["results"]["http"]
    if http:
        print(f"{'route class':<16} {'count':>6} {'p50ms':>7} {'p95ms':>7} {'p99ms':>7} statuses")
        for route_class, row in http.items():
            statuses = ",".join(f"{k}:{v}" for k, v in row["statuses"].items())
            print(
                f"{route_class:<16} {row['count']:>6} {row['p50_ms']:>7} {row['p95_ms']:>7} "
                f"{row['p99_ms']:>7} {statuses}"
            )
        substituted = report["results"]["substituted_writes"]
        print(f"writes substituted by pacing: {substituted}")
    instrument = report["results"].get("instrument", {})
    verdict = instrument.get("verdict")
    if verdict == "saturated":
        print(
            f"INSTRUMENT WARNING: mix read p95 is {instrument['ratio']}x the reference "
            f"probe's {instrument['probe_p95_ms']}ms. This flags inconsistent latency, "
            "not its cause; correlate driver and server telemetry before capacity conclusions."
        )
    elif verdict == "clean":
        print(
            f"instrument consistency heuristic: ratio {instrument['ratio']}x the "
            f"probe's {instrument['probe_p95_ms']}ms; this does not exclude driver saturation"
        )
    elif verdict == "no_probe":
        print("instrument self-audit: NO PROBE SAMPLES - treat every latency above with suspicion")
    ws = report["results"]["ws"]
    if ws["attempts"]:
        named = ws.get("close_codes_named", ws["close_codes"])
        print(
            f"ws: {ws['connected']}/{ws['attempts']} upgraded, {ws['ready']} ready; "
            f"settled {ws['settled_for_failure_rate']}, failure {ws['failure_pct']}%; "
            f"outcomes {ws['outcomes']}; closes {named}"
        )
    print(f"HTTP + ready WS overlap: {report['coverage']['http_ready_ws_overlap']}")
    print("Receipt delivery: NOT_PROVEN; production capacity: NOT_PROVEN")
