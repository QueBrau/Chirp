"""CLI entry: python -m loadtest --config <yaml> --manifest <json> [--out report.json]."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import time

from loadtest.accounts import load_manifest
from loadtest.config import load_config
from loadtest.report import build_report, print_summary, write_report
from loadtest.runner import Runner
from loadtest.ws_leg import run_ws_phase


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="loadtest",
        description=(
            "Chirp load-test harness (board c226). Target defaults to localhost; a "
            "non-local target additionally requires an approval block in the config "
            "AND --confirm-park-lifted, because running against prod is parked by Jose."
        ),
    )
    parser.add_argument("--config", required=True, help="YAML config with caps + abort criteria")
    parser.add_argument("--manifest", required=True, help="users manifest JSON")
    parser.add_argument("--out", default="loadtest-report.json", help="report JSON path")
    parser.add_argument("--users", type=int, default=0, help="use only the first N manifest users")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="override duration_seconds from the config"
    )
    parser.add_argument("--http-only", action="store_true", help="skip the WS connect-storm leg")
    parser.add_argument("--ws-only", action="store_true", help="run only the WS connect-storm leg")
    parser.add_argument(
        "--confirm-park-lifted",
        action="store_true",
        help="required (with a config approval block) for any non-local target",
    )
    args = parser.parse_args()

    config = load_config(args.config, confirm_park_lifted=args.confirm_park_lifted)
    if args.duration > 0:
        config = dataclasses.replace(config, duration_seconds=args.duration)
        config.validate(confirm_park_lifted=args.confirm_park_lifted)
    manifest = load_manifest(args.manifest, auth_mode=config.auth_mode)
    if args.users > 0:
        manifest = dataclasses.replace(manifest, users=manifest.users[: args.users])
    if args.http_only and args.ws_only:
        raise SystemExit("--http-only and --ws-only are mutually exclusive")

    runner = Runner(config, manifest)
    phases: list[str] = []
    started = time.monotonic()

    async def _run() -> None:
        if not args.ws_only:
            phases.append("http_mix")
            await runner.run_http_phase()
        # The WS leg is skipped after an HTTP-phase abort: criteria tripping is a
        # stop-everything signal, not a phase boundary.
        if not args.http_only and not runner.abort_violations:
            phases.append("ws_storm")
            runner.stop.clear()
            await run_ws_phase(config, manifest, runner.recorder, runner.stop)
            violations = runner.monitor.check(time.monotonic() - started)
            ws_violations = [v for v in violations if v.criterion == "ws_failure_pct"]
            runner.abort_violations.extend(ws_violations)

    asyncio.run(_run())
    report = build_report(
        config,
        runner.recorder,
        runner.abort_violations,
        phases_run=phases,
        wall_seconds=time.monotonic() - started,
    )
    write_report(report, args.out)
    print_summary(report)
    print(f"report written to {args.out}")
    return 2 if runner.abort_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
