"""Bounded LOCAL cohort reads/cardinality and HTTP-created WebSocket receipts.

This fixed synthetic plan is not the proposed staging mix or fairness acceptance.
It neither follows redirects nor accepts non-literal-loopback targets.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import os
import time

import httpx

from loadtest.abort import AbortMonitor
from loadtest.accounts import auth_headers
from loadtest.cohort_contract import (
    MAX_MANIFEST_BYTES, MAX_RESPONSE_BYTES, PAGE_SIZE, read_manifest, validate_cohort_run,
)
from loadtest.config import ConfigError, load_config
from loadtest.message_receipts import Observation, PrivateArgumentParser
from loadtest.metrics import Sample, quantile
from loadtest.pacing import Pacer, TokenBucket
from loadtest.receipt_contract import (
    MAX_HTTP_SAMPLES, OVERALL_TIMEOUT_SECONDS, ReceiptError, canonical_uuid, strict_json, timestamp,
)
from loadtest.runner import REQUEST_TIMEOUT, Runner

READ_ROUTES = ("campus_feed", "chapter_posts", "inbox", "history", "roster", "hot_inbox")


def distribution(values):
    ordered = sorted(values)
    return {"samples": len(ordered), "p50": round(quantile(ordered, .5), 3),
            "p95": round(quantile(ordered, .95), 3), "max": round(max(ordered, default=0), 3)}


class Shared:
    def __init__(self, config, count):
        self.pacer = Pacer(config.caps.max_rps, config.caps.max_concurrent_requests,
                           config.caps.per_user_writes_per_minute)
        self.connects = TokenBucket(config.ws.connects_per_second, burst=1)
        self.send_lock = asyncio.Lock()
        self.previous_send = None
        self.states = []
        self.count = count
        self.http_ready = set()
        self.all_ready = asyncio.Event()
        self.requests = 0
        self.lag_ms = []

    async def audit_loop(self):
        while len(self.lag_ms) < 2000:
            expected = time.monotonic() + .1
            await asyncio.sleep(.1)
            self.lag_ms.append(max(0, time.monotonic() - expected) * 1000)


class PhaseExpired(Exception):
    pass


class CohortRunner(Runner):
    def __init__(self, state, cohort, shared):
        super().__init__(state.config, cohort.receipt.http)
        self.state, self.cohort, self.shared = state, cohort, shared
        self.recorder = state.recorder
        self.monitor = AbortMonitor(self.config.abort, self.recorder)
        self.pacer = shared.pacer
        self.deadline = None
        self.metrics = {user.uid: {"routes": Counter(), "route_stats": {}, "errors": 0, "latency_ms": [],
                                   "queue_ms": [], "walks": Counter()} for user in self.manifest.users}

    def record_request(self, user, route, status, latency_ms, queue_ms):
        metric = self.metrics[user.uid]
        metric["routes"][route] += 1
        metric["errors"] += int(not 200 <= status < 300)
        metric["latency_ms"].append(latency_ms)
        metric["queue_ms"].append(queue_ms)
        detail = metric["route_stats"].setdefault(route, {"statuses": Counter(), "latency_ms": [], "queue_ms": []})
        detail["statuses"][status] += 1
        detail["latency_ms"].append(latency_ms)
        detail["queue_ms"].append(queue_ms)

    async def get(self, client, user, route, path, params=None):
        queued = time.monotonic()
        await self.pacer.global_bucket.acquire()
        async with self.pacer.semaphore:
            if self.stop.is_set() or time.monotonic() >= self.deadline:
                raise PhaseExpired
            if self.shared.requests >= MAX_HTTP_SAMPLES:
                raise ReceiptError("aggregate_http_sample_budget_exceeded")
            self.shared.requests += 1
            started, status = time.monotonic(), 0
            try:
                async with client.stream("GET", path, params=params,
                                         headers=auth_headers(user, "emulated")) as response:
                    status = response.status_code
                    if status != 200:
                        raise ReceiptError("cohort_read_not_200")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ReceiptError("cohort_response_too_large")
                    return strict_json(bytes(body), limit=MAX_RESPONSE_BYTES)
            except httpx.HTTPError:
                raise ReceiptError("cohort_read_transport_failure") from None
            finally:
                elapsed = (time.monotonic() - started) * 1000
                self.record_request(user, route, status, elapsed, (started - queued) * 1000)
                self.recorder.record(Sample(self._now(), route, status, elapsed))

    def validate_row(self, row, route):
        if not isinstance(row, dict):
            raise ReceiptError("cohort_response_shape_invalid")
        identifier = canonical_uuid(row.get("id"))
        receipt = self.cohort.receipt
        if route in {"campus_feed", "chapter_posts"}:
            valid = (row.get("chapter_id") == receipt.http.chapter_id
                     and row.get("campus_id") == receipt.http.campus_id
                     and row.get("audience") == ("campus" if route == "campus_feed" else "org")
                     and row.get("author_id") in self.cohort.active_ids)
        elif route in {"inbox", "hot_inbox"}:
            members = row.get("members")
            valid = (row.get("chapter_id") == receipt.http.chapter_id and row.get("kind") == "group"
                     and isinstance(members, list) and len(members) == len(self.cohort.active_ids)
                     and all(isinstance(member, dict) and member.get("conversation_id") == identifier
                             for member in members)
                     and {member.get("user_id") for member in members} == self.cohort.active_ids)
        else:
            valid = (row.get("conversation_id") == receipt.conversation_id
                     and row.get("sender_device_id") == receipt.sender_device_id)
        if not valid:
            raise ReceiptError("cohort_response_identity_mismatch")
        return identifier, timestamp(row.get("created_at"))

    async def walk(self, client, user, route, path, expected, *, first_page_only=False):
        params = {"limit": PAGE_SIZE}
        if route == "history":
            params["before"] = self.cohort.history_before
        seen, previous = set(), None
        # One extra page would be needed only for an exact multiple. Profiles
        # deliberately exercise 51/201 rows against a 50-row page boundary.
        for _ in range((len(expected) + PAGE_SIZE - 1) // PAGE_SIZE + 1):
            rows = await self.get(client, user, route, path, params)
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise ReceiptError("cohort_page_shape_invalid")
            for row in rows:
                identifier, created = self.validate_row(row, route)
                current = (created, identifier)
                if identifier not in expected or identifier in seen:
                    raise ReceiptError("cohort_page_identity_mismatch")
                if previous is not None and current >= previous:
                    raise ReceiptError("cohort_cursor_not_descending")
                seen.add(identifier)
                previous = current
            if first_page_only:
                if len(rows) != min(PAGE_SIZE, len(expected)):
                    raise ReceiptError("cohort_hot_inbox_incomplete")
                return
            if len(rows) < PAGE_SIZE:
                if seen != expected:
                    raise ReceiptError("cohort_pagination_incomplete")
                self.metrics[user.uid]["walks"][route] += 1
                return
            params = {"limit": PAGE_SIZE, "before": rows[-1]["created_at"], "before_id": rows[-1]["id"]}
        raise ReceiptError("cohort_pagination_budget_exceeded")

    async def exercise(self, client, user):
        receipt = self.cohort.receipt
        campus, chapter = receipt.http.campus_id, receipt.http.chapter_id
        for route, path, expected in (
            ("campus_feed", f"/campuses/{campus}/feed", self.cohort.post_ids),
            ("chapter_posts", f"/chapters/{chapter}/posts", self.cohort.chapter_post_ids),
            ("inbox", "/conversations", self.cohort.conversation_ids),
            ("history", f"/conversations/{receipt.conversation_id}/messages", self.cohort.message_ids),
        ):
            await self.walk(client, user, route, path, expected)
        roster = await self.get(client, user, "roster", f"/chapters/{chapter}/members")
        if (not isinstance(roster, list) or len(roster) != len(self.cohort.member_ids)
                or any(not isinstance(row, dict) or row.get("chapter_id") != chapter for row in roster)
                or {row.get("user_id") for row in roster} != self.cohort.member_ids):
            raise ReceiptError("cohort_roster_mismatch")
        self.metrics[user.uid]["walks"]["roster"] += 1
        for _ in range(3):
            await self.walk(client, user, "hot_inbox", "/conversations", self.cohort.conversation_ids,
                            first_page_only=True)
        self.metrics[user.uid]["walks"]["hot_inbox"] += 1

    async def run_http_phase(self):
        self.shared.http_ready.add(self.state.ordinal)
        if len(self.shared.http_ready) == self.shared.count:
            self.shared.all_ready.set()
        await self.state.until_stop(self.shared.all_ready.wait())
        self.deadline = time.monotonic() + self.config.duration_seconds
        async with httpx.AsyncClient(
            base_url=self.config.base_url, timeout=REQUEST_TIMEOUT, trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=self.config.caps.max_concurrent_requests,
                               max_keepalive_connections=self.config.caps.max_concurrent_requests),
        ) as client:
            self._http_client = client
            self.recorder.set_http_mix_active(True)
            try:
                index = 0
                while not self.stop.is_set():
                    await self.exercise(client, self.manifest.users[index % len(self.manifest.users)])
                    index += 1
                    await asyncio.sleep(self.config.think_seconds)
            except PhaseExpired:
                pass
            except ReceiptError as error:
                self.state.fail(str(error))
            finally:
                self.recorder.set_http_mix_active(False)

    def coverage_complete(self):
        return all(all(metric["walks"][route] > 0 for route in READ_ROUTES) for metric in self.metrics.values())

    def member_report(self):
        return [{"member": index + 1, "requests": sum(metric["routes"].values()),
                 "routes": dict(metric["routes"]), "errors": metric["errors"],
                 "route_stats": {route: {"statuses": dict(detail["statuses"]),
                                         "client_response_ms": distribution(detail["latency_ms"]),
                                         "scheduler_wait_ms": distribution(detail["queue_ms"])}
                                 for route, detail in metric["route_stats"].items()},
                 "completed_walks": dict(metric["walks"]),
                 "client_response_ms": distribution(metric["latency_ms"]),
                 "scheduler_wait_ms": distribution(metric["queue_ms"])}
                for index, metric in enumerate(self.metrics.values())]


class CohortObservation(Observation):
    def __init__(self, config, cohort, shared, ordinal, messages, interval, settle):
        super().__init__(config, cohort.receipt, messages, interval, settle)
        self.shared, self.ordinal, self.cohort = shared, ordinal, cohort
        self.runner = CohortRunner(self, cohort, shared)

    def fail(self, code):
        super().fail(code)
        for state in self.shared.states:
            state.runner.stop.set()

    async def receiver(self, index):
        await self.shared.connects.acquire()
        await super().receiver(index)

    async def send_one(self, client, previous_start):
        queued, before = time.monotonic(), len(self.attempts)
        async with self.shared.send_lock:
            try:
                return await super().send_one(client, self.shared.previous_send)
            finally:
                if len(self.attempts) > before:
                    attempt = self.attempts[-1]
                    self.shared.previous_send = attempt.started_at
                    self.runner.record_request(
                        self.manifest.sender, "message_send", 201 if attempt.status == "accepted" else 0,
                        (time.monotonic() - attempt.started_at) * 1000, (attempt.started_at - queued) * 1000,
                    )


async def run_cohorts(config, cohorts, *, messages=1, interval_seconds=4, settle_seconds=2):
    validate_cohort_run(config, cohorts, messages, interval_seconds, settle_seconds)
    started, cpu_started = time.monotonic(), time.process_time()
    shared = Shared(config, len(cohorts))
    shared.states = [CohortObservation(config, cohort, shared, index + 1, messages,
                                      interval_seconds, settle_seconds)
                     for index, cohort in enumerate(cohorts)]
    audit = asyncio.create_task(shared.audit_loop(), name="cohort-driver-audit")
    tasks = [asyncio.create_task(state.run(), name="local-cohort") for state in shared.states]
    errors = set()
    try:
        _, pending = await asyncio.wait(tasks, timeout=OVERALL_TIMEOUT_SECONDS - 6)
        if pending:
            errors.add("aggregate_wall_deadline_exceeded")
    except asyncio.CancelledError:
        errors.add("run_cancelled")
    finally:
        if errors:
            for state in shared.states:
                state.fail("aggregate_run_stopped")
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            async with asyncio.timeout(6):
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, asyncio.CancelledError):
                        errors.add("cohort_task_cancelled")
                    elif isinstance(result, BaseException):
                        errors.add("cohort_task_failed")
        except (TimeoutError, asyncio.CancelledError):
            errors.add("aggregate_cleanup_deadline_exceeded")
        audit.cancel()
        audit_results = await asyncio.gather(audit, return_exceptions=True)
        if any(isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
               for result in audit_results):
            errors.add("driver_audit_failed")
    reports = []
    for state in shared.states:
        receipt_report = state.report()
        # This custom read leg starts NO independent reference clients.
        receipt_report["budgets"].update(reference_probe_additional_rps=0, reference_probe_additional_inflight=0)
        complete = state.runner.coverage_complete()
        if not complete:
            errors.add("cohort_read_coverage_incomplete")
        if receipt_report["status"] != "LOCAL_RECEIPTS_OBSERVED":
            errors.add("cohort_receipts_not_proven")
        members = state.runner.member_report()
        reports.append({"cohort": state.ordinal, "campus": state.ordinal, "chapter": state.ordinal,
                        "active_users": len(members), "selected_recipients": len(state.manifest.recipients),
                        "fixture_counts": {"chapter_members": len(state.cohort.member_ids),
                                           "campus_posts": len(state.cohort.post_ids),
                                           "chapter_posts": len(state.cohort.chapter_post_ids),
                                           "conversations": len(state.cohort.conversation_ids),
                                           "historical_messages": len(state.cohort.message_ids)},
                        "read_coverage_complete": complete, "members": members,
                        "requests": sum(member["requests"] for member in members), "delivery": receipt_report})
    total = sum(report["requests"] for report in reports)
    for report in reports:
        report["observed_request_share_pct"] = round(100 * report["requests"] / total, 3) if total else None
    return {"schema_version": 1, "status": "LOCAL_COHORT_MIX_OBSERVED" if not errors else "NOT_PROVEN",
            "errors": sorted(errors), "cohorts": reports,
            "workload": "fixed_cursor_walks_roster_three_paced_inbox_refreshes_and_message_receipts",
            "read_scheduling": "one_sequential_reader_per_cohort_rotating_selected_member_identities",
            "fairness": {"meaning": "observed_request_shares_of_selected_synthetic_cohorts_only",
                         "campus_chapter_mapping": "one_isolated_chapter_per_campus",
                         "requests": total, "acceptance_proven": False},
            "budgets": {"aggregate_max_rps": config.caps.max_rps,
                        "aggregate_initial_token_burst": max(2, config.caps.max_rps),
                        "aggregate_max_inflight": config.caps.max_concurrent_requests,
                        "aggregate_reference_extra_rps": 0, "aggregate_reference_extra_inflight": 0,
                        "aggregate_ws_connects_per_second": config.ws.connects_per_second,
                        "aggregate_message_interval_seconds": interval_seconds,
                        "requested_messages": len(cohorts) * messages,
                        "max_response_bytes": MAX_RESPONSE_BYTES, "page_size": PAGE_SIZE,
                        "wall_timeout_seconds": OVERALL_TIMEOUT_SECONDS},
            "driver": {"elapsed_seconds": round(time.monotonic() - started, 3),
                       "process_cpu_seconds": round(time.process_time() - cpu_started, 3),
                       "event_loop_delay_ms": distribution(shared.lag_ms),
                       "meaning": "this_python_process_only_not_server_latency_or_driver_clearance"},
            "not_proven": ["photos", "polls", "dues", "representative_staging_mix", "production_capacity",
                           "fairness_acceptance", "warm_cold_server_control", "server_latency_and_pool_waits",
                           "dependency_process_restore_recovery", "device_decryption", "native_device_acceptance"]}


def main():
    parser = PrivateArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--messages", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=4)
    parser.add_argument("--settle-seconds", type=float, default=2)
    try:
        args = parser.parse_args()
        with open(args.config, "rb") as handle:
            if len(handle.read(MAX_MANIFEST_BYTES + 1)) > MAX_MANIFEST_BYTES:
                raise ReceiptError("config_too_large")
        try:
            config = load_config(args.config)
        except (ConfigError, Exception):
            raise ReceiptError("harness_config_invalid") from None
        cohorts = read_manifest(args.manifest)
        validate_cohort_run(config, cohorts, args.messages, args.interval_seconds, args.settle_seconds)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            result = asyncio.run(run_cohorts(config, cohorts, messages=args.messages,
                                            interval_seconds=args.interval_seconds,
                                            settle_seconds=args.settle_seconds))
            json.dump(result, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except ReceiptError as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"status": "NOT_PROVEN", "reason": "input_run_or_output_failed"}))
        return 2
    print(json.dumps({"status": result["status"], "cohorts": len(cohorts)}))
    return 0 if result["status"] == "LOCAL_COHORT_MIX_OBSERVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
