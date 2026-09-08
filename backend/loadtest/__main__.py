"""CLI entry: python -m loadtest --config <yaml> --manifest <json> [--out report.json]."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import math
import time

from loadtest.accounts import load_manifest
from loadtest.config import load_config
from loadtest.report import build_report, print_summary, write_report
from loadtest.runner import Runner
from loadtest.ws_leg import run_ws_phase


async def run_phases(runner: Runner, phases: list[str]) -> None:
    """One abort owner, concurrent selected legs, and no hidden child exceptions."""
    remaining = len(phases)
    legs: list[asyncio.Task] = []

    async def leg(name: str) -> None:
        nonlocal remaining
        try:
            if name == "http_mix":
                await runner.run_http_phase()
            else:
                await run_ws_phase(runner.config, runner.manifest, runner.recorder, runner.stop)
        finally:
            remaining -= 1
            if remaining == 0:
                runner.stop.set()

    async def watch() -> None:
        await runner._abort_watch()
        for task in legs:
            if not task.done():
                task.cancel()

    try:
        async with asyncio.TaskGroup() as group:
            legs.extend(group.create_task(leg(name), name=f"loadtest-{name}") for name in phases)
            group.create_task(watch(), name="loadtest-abort")
    finally:
        runner.stop.set()
    # A late close may settle after the last periodic tick. Same grace/minimum apply.
    if not runner.abort_violations:
        runner.abort_violations.extend(runner.monitor.check(runner._now()))


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
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and non-negative")

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
    phases = ([] if args.ws_only else ["http_mix"]) + ([] if args.http_only else ["ws_storm"])
    started = time.monotonic()
    run_status = "completed"
    try:
        asyncio.run(run_phases(runner, phases))
    except KeyboardInterrupt:
        run_status = "interrupted"
    except Exception:
        # Preserve a report and nonzero exit without printing target exception bodies.
        run_status = "internal_error"
    report = build_report(
        config,
        runner.recorder,
        runner.abort_violations,
        phases_run=phases,
        wall_seconds=time.monotonic() - started,
        run_status=run_status,
    )
    write_report(report, args.out)
    print_summary(report)
    print(f"report written to {args.out}")
    if run_status == "interrupted":
        return 130
    return 2 if runner.abort_violations or report["run_status"] != "completed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
