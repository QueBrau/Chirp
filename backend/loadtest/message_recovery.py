"""Opt-in loopback-only proof of deliberate client disconnect/history recovery.

Exactly three opaque synthetic messages: live, all recipients offline, live again.
This does not test Redis/process failure, native clients or production capacity.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
from websockets.protocol import State

from loadtest.abort import AbortMonitor
from loadtest.accounts import auth_headers
from loadtest.config import ConfigError, HarnessConfig, load_config
from loadtest.message_receipts import Observation, PrivateArgumentParser, ReceiptRecorder, payload_hash
from loadtest.metrics import REFERENCE_CLASS, Sample, quantile
from loadtest.receipt_contract import (
    MAX_INPUT_BYTES, MAX_TOTAL_FRAMES, OVERALL_TIMEOUT_SECONDS, ReceiptError,
    ReceiptManifest, canonical_uuid, keys, read_manifest, strict_json, timestamp, validate_run,
)
from loadtest.runner import REQUEST_TIMEOUT
from loadtest.ws_leg import CLOSE_TIMEOUT_SECONDS, OPEN_TIMEOUT_SECONDS, READY_TIMEOUT_SECONDS

MAX_HISTORY_ROWS = 20
MESSAGE_FIELDS = {"id", "conversation_id", "sender_device_id", "ciphertext_b64", "message_type", "created_at"}
SUCCESS = "LOCAL_RECONNECT_RECOVERY_OBSERVED"


class RecoveryRecorder(ReceiptRecorder):
    def record(self, sample: Sample) -> None:
        super().record(sample)
        state = self.state
        if (self.active and sample.route_class != REFERENCE_CLASS and 200 <= sample.status < 300
                and (state.cohort_open() or state.phase == "offline") and state.phase in state.phase_mix
                and state.phase_started is not None
                and state.runner._t0 + sample.at >= state.phase_started
                and (state.deadline is None or state.runner._t0 + sample.at <= state.deadline)):
            state.phase_mix[state.phase] += 1


class RecoveryObservation(Observation):
    def __init__(self, config: HarnessConfig, manifest: ReceiptManifest,
                 interval_seconds: float, settle_seconds: float) -> None:
        super().__init__(config, manifest, 3, interval_seconds, settle_seconds)
        self.recorder = RecoveryRecorder(config.abort.window_seconds, self)
        self.runner.recorder = self.recorder
        self.runner.monitor = AbortMonitor(config.abort, self.recorder)
        self.phase = "initial_live"
        self.phase_started: float | None = None
        self.phase_mix = {"initial_live": 0, "offline": 0, "final_live": 0}
        self.phases = {name: False for name in (
            "initial_live", "recipients_offline", "reconnected_ready", "history_reconciled", "final_live",
        )}
        self.expected_closes = {}
        self.receiver_tasks = []
        self.history_seen: set[int] = set()
        self.history_requests = 0
        self.history_rows = 0
        self.history_latencies = []
        self.delivery_changed = asyncio.Event()
        self.live_attempts = []
        self.offline_attempt = None
        self.intentional_closed = 0
        self.recovery_finished = False

    async def receiver(self, index: int) -> None:
        self.receiver_tasks.append(asyncio.current_task())
        await super().receiver(index)

    def expected_receiver_close(self, index: int, connection) -> bool:
        # Bind the exception to this old connection, this transition and a
        # locally initiated normal handshake. A racing 4503/1006 stays a failure.
        if (self.phase != "disconnecting" or connection is None
                or self.expected_closes.get(index) is not connection):
            return False
        protocol = connection.protocol
        return bool(protocol.close_sent and protocol.close_sent.code == 1000
                    and protocol.close_rcvd and protocol.close_rcvd.code == 1000
                    and protocol.close_rcvd_then_sent is False)

    def validate_send_context(self) -> None:
        if self.phase == "offline":
            if (self.runner.stop.is_set() or not self.phases["recipients_offline"]
                    or len(self.connections) != len(self.ready)
                    or any(connection.state is not State.CLOSED for connection in self.connections.values())
                    or any(not task.done() for task in self.receiver_tasks)):
                raise ReceiptError("offline_cohort_not_proven")
            if not self.recorder.active:
                raise ReceiptError("http_mix_ended_before_send")
        else:
            super().validate_send_context()
            if self.phase not in ("initial_live", "final_live"):
                raise ReceiptError("message_phase_invalid")
            if self.phase == "final_live" and not self.phases["history_reconciled"]:
                raise ReceiptError("history_not_reconciled_before_final_send")
            self.phase_started = time.monotonic()

    def reconcile(self, event, attempt) -> None:
        super().reconcile(event, attempt)
        self.delivery_changed.set()

    async def live_send(self, client: httpx.AsyncClient, previous: float | None) -> float:
        if previous is not None:
            await asyncio.sleep(max(0, previous + self.interval - time.monotonic()))
        started = await self.send_one(client, previous)
        attempt = self.attempts[-1]
        self.live_attempts.append(attempt)
        deadline = self.deadline if self.deadline is not None else time.monotonic() + self.settle
        # Receipt timestamps, not the order in which deadline/receiver tasks
        # resume, decide whether a late frame can satisfy this live phase.
        self.deadline = deadline
        expected = {(index, attempt.message_id) for index in range(len(self.ready))}
        while not expected <= self.received:
            self.delivery_changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReceiptError("live_phase_receipts_missing")
            try:
                await asyncio.wait_for(self.delivery_changed.wait(), remaining)
            except TimeoutError:
                raise ReceiptError("live_phase_receipts_missing") from None
        # Keep the full observation window, including real mix responses and
        # duplicate/late frames, rather than closing as soon as an event arrives.
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        if not self.phase_mix[self.phase]:
            raise ReceiptError("live_phase_http_overlap_missing")
        if not self.cohort_open():
            raise ReceiptError("live_phase_cohort_lost")
        self.phases[self.phase] = True
        return started

    async def disconnect(self) -> None:
        if not self.phases["initial_live"] or not self.cohort_open():
            raise ReceiptError("initial_live_phase_not_proven")
        old_tasks = list(self.receiver_tasks)
        self.phase = "disconnecting"
        self.expected_closes = dict(self.connections)
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS + 1):
                await asyncio.gather(*(connection.close(code=1000) for connection in self.connections.values()))
                await asyncio.gather(*old_tasks)
        except TimeoutError:
            raise ReceiptError("intentional_close_timeout") from None
        if (self.errors or any(connection.state is not State.CLOSED for connection in self.connections.values())
                or any(not self.expected_receiver_close(index, connection)
                       for index, connection in self.connections.items())):
            raise ReceiptError("intentional_close_not_proven")
        self.intentional_closed = len(self.connections)
        self.expected_closes.clear()
        self.phases["recipients_offline"] = True
        self.phase = "offline"
        self.phase_started = time.monotonic()
        self.deadline = None

    async def reconnect(self) -> None:
        if self.offline_attempt is None or self.offline_attempt.status != "accepted":
            raise ReceiptError("offline_message_not_accepted")
        self.phase = "reconnecting"
        self.ready = [asyncio.Event() for _ in self.manifest.recipients]
        self.connections = {}
        self.receiver_tasks = []
        # One bounded generation, no retry: each fresh ready belongs to a new
        # connection and old ready events can never satisfy this fence.
        budget = ((len(self.ready) - 1) / self.config.ws.connects_per_second
                  + OPEN_TIMEOUT_SECONDS + READY_TIMEOUT_SECONDS + 1)
        try:
            async with asyncio.timeout(budget):
                for index in range(len(self.ready)):
                    self.tasks.append(asyncio.create_task(self.receiver(index), name="recovery-receiver"))
                    if index + 1 < len(self.ready):
                        await asyncio.sleep(1 / self.config.ws.connects_per_second)
                await asyncio.gather(*(event.wait() for event in self.ready))
        except TimeoutError:
            raise ReceiptError("reconnect_ready_timeout") from None
        if not self.cohort_open():
            raise ReceiptError("reconnected_cohort_unavailable")
        self.phases["reconnected_ready"] = True

    async def history(self, client: httpx.AsyncClient) -> None:
        self.phase = "history"
        attempt = self.offline_attempt
        for index, recipient in enumerate(self.manifest.recipients):
            await self.runner.pacer.global_bucket.acquire()
            async with self.runner.pacer.semaphore:
                if self.runner.stop.is_set() or not self.cohort_open():
                    raise ReceiptError("history_cohort_unavailable")
                if not self.recorder.active:
                    raise ReceiptError("http_mix_ended_before_history")
                started = time.monotonic()
                self.history_requests += 1
                async with client.stream(
                    "GET", f"/conversations/{self.manifest.conversation_id}/messages",
                    params={"limit": MAX_HISTORY_ROWS}, headers=auth_headers(recipient, "emulated"),
                ) as response:
                    if response.status_code != 200:
                        raise ReceiptError("history_not_200")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_INPUT_BYTES:
                            raise ReceiptError("history_response_too_large")
                    rows = strict_json(bytes(raw))
                if not isinstance(rows, list) or len(rows) > MAX_HISTORY_ROWS:
                    raise ReceiptError("history_shape_invalid")
                self.history_rows += len(rows)
                matches = 0
                seen = set()
                for row in rows:
                    keys(row, MESSAGE_FIELDS)
                    identifier = canonical_uuid(row["id"])
                    if identifier in seen:
                        raise ReceiptError("history_duplicate_message")
                    seen.add(identifier)
                    if identifier != attempt.message_id:
                        continue
                    if (row["conversation_id"] != self.manifest.conversation_id
                            or row["sender_device_id"] != self.manifest.sender_device_id
                            or row["message_type"] != "signal"
                            or payload_hash(row["ciphertext_b64"]) != attempt.payload_hash
                            or timestamp(row["created_at"]) != attempt.created_at):
                        raise ReceiptError("history_binding_mismatch")
                    matches += 1
                if matches != 1:
                    raise ReceiptError("history_accepted_message_missing")
                self.history_seen.add(index)
                self.history_latencies.append((time.monotonic() - started) * 1000)
        self.phases["history_reconciled"] = len(self.history_seen) == len(self.ready)

    async def producer(self) -> None:
        try:
            await self.recorder.first_mix_response.wait()
            async with httpx.AsyncClient(
                base_url=self.config.base_url, timeout=REQUEST_TIMEOUT, trust_env=False,
                follow_redirects=False, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            ) as client:
                previous = await self.live_send(client, None)
                await self.disconnect()
                await asyncio.sleep(max(0, previous + self.interval - time.monotonic()))
                previous = await self.send_one(client, previous)
                self.offline_attempt = self.attempts[-1]
                if not self.phase_mix["offline"]:
                    raise ReceiptError("offline_phase_http_overlap_missing")
                await self.reconnect()
                await self.history(client)
                self.phase, self.phase_started = "final_live", None
                await self.live_send(client, previous)
                self.recovery_finished = True
                self.phase = "complete"
        except asyncio.CancelledError:
            if not self.finishing:
                self.fail("recovery_producer_cancelled")
            raise
        except ReceiptError as error:
            self.fail(str(error))
        except httpx.HTTPError:
            self.fail("history_request_failed")
        except Exception:
            self.fail("recovery_internal_failure")

    def report(self) -> dict:
        cohort = len(self.manifest.recipients)
        live_ids = {attempt.message_id for attempt in self.live_attempts if attempt.status == "accepted"}
        live_unique = sum(identifier in live_ids for _, identifier in self.received)
        missing_live, missing_history = 2 * cohort - live_unique, cohort - len(self.history_seen)
        if missing_live:
            self.errors.add("live_receipts_missing")
        if missing_history:
            self.errors.add("history_recovery_missing")
        if not self.recovery_finished or not all(self.phases.values()) or len(self.accepted) != 3:
            self.errors.add("recovery_sequence_incomplete")
        if not self.http_completed:
            self.errors.add("http_phase_incomplete")
        if not all(self.phase_mix[phase] for phase in ("initial_live", "final_live")):
            self.errors.add("live_phase_http_overlap_missing")
        if not self.phase_mix["offline"]:
            self.errors.add("offline_phase_http_overlap_missing")
        if self.runner.abort_violations:
            self.errors.add("http_abort_threshold_exceeded")
        intervals = [after.started_at - before.started_at for before, after in zip(self.attempts, self.attempts[1:])]
        if any(value < self.interval for value in intervals):
            self.errors.add("message_spacing_invalid")
        return {
            "schema_version": 1, "status": SUCCESS if not self.errors else "NOT_PROVEN",
            "scope": "selected_manifest_recipients_deliberate_client_disconnect",
            "errors": sorted(self.errors), "recipient_count": cohort, "phases": dict(self.phases),
            "messages": {"requested": 3, "attempted": len(self.attempts), "accepted": len(self.accepted),
                         "rejected": sum(attempt.status == "rejected" for attempt in self.attempts),
                         "unconfirmed": sum(attempt.status == "unconfirmed" for attempt in self.attempts)},
            "live_receipts": {"expected": 2 * cohort, "unique": live_unique, "missing": missing_live,
                              **self.counts},
            "history_recovery": {"expected": cohort, "unique": len(self.history_seen), "missing": missing_history,
                                 "requests": self.history_requests, "rows_examined": self.history_rows,
                                 "request_to_validated_history_p95_ms": round(quantile(sorted(self.history_latencies), .95), 3)},
            "offline_message_ws_observations": sum(
                identifier == self.offline_attempt.message_id for _, identifier in self.received
            ) if self.offline_attempt else 0,
            "intentional_recipient_closes": self.intentional_closed,
            "phase_http_mix_2xx": dict(self.phase_mix),
            "actual_dispatch_intervals_seconds": intervals,
            "budgets": {"http_duration_seconds": self.config.duration_seconds,
                        "wall_timeout_seconds": OVERALL_TIMEOUT_SECONDS,
                        "shared_max_rps": self.config.caps.max_rps,
                        "shared_initial_token_burst": max(2, self.config.caps.max_rps),
                        "shared_max_inflight": self.config.caps.max_concurrent_requests,
                        "reference_probe_additional_rps": 1, "reference_probe_additional_inflight": 1,
                        "message_interval_seconds": self.interval, "settle_seconds": self.settle,
                        "max_history_requests": cohort, "max_history_rows_per_response": MAX_HISTORY_ROWS,
                        "max_history_response_bytes": MAX_INPUT_BYTES, "max_total_receipt_frames": MAX_TOTAL_FRAMES},
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
            "not_proven": ["production_capacity", "redis_failure_recovery", "process_failure_recovery",
                           "device_decryption", "phone_reconnect_recovery", "server_receipt_rows",
                           "all_conversation_members", "exactly_once_delivery", "historical_restore_rpo_rto"],
            "http_and_ws": self.recorder.summary(),
        }


async def run_recovery(config: HarnessConfig, manifest: ReceiptManifest, *,
                       interval_seconds: float = 4, settle_seconds: float = 2) -> dict:
    return await RecoveryObservation(config, manifest, interval_seconds, settle_seconds).run()


def main() -> int:
    parser = PrivateArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True, help="New private aggregate JSON file, never overwritten")
    parser.add_argument("--interval-seconds", type=float, default=4)
    parser.add_argument("--settle-seconds", type=float, default=2)
    try:
        args = parser.parse_args()
        with open(args.config, "rb") as handle:
            if len(handle.read(MAX_INPUT_BYTES + 1)) > MAX_INPUT_BYTES:
                raise ReceiptError("config_too_large")
        try:
            config = load_config(args.config)
        except (ConfigError, Exception):
            raise ReceiptError("harness_config_invalid") from None
        manifest = read_manifest(args.manifest)
        validate_run(config, manifest, 3, args.interval_seconds, args.settle_seconds)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            result = asyncio.run(run_recovery(config, manifest, interval_seconds=args.interval_seconds,
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
    print(json.dumps({"status": result["status"], "accepted_messages": result["messages"]["accepted"],
                      "live_missing": result["live_receipts"]["missing"],
                      "history_missing": result["history_recovery"]["missing"]}))
    return 0 if result["status"] == SUCCESS else 2


if __name__ == "__main__":
    raise SystemExit(main())
