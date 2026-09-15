"""Opt-in LOCAL-ONLY HTTP/message/selected-recipient receipt observation.

This sends opaque synthetic bytes, not encrypted client messages. It proves
neither production capacity, durable recovery, decryption nor server receipt rows.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import time

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.protocol import State

from loadtest.abort import AbortMonitor
from loadtest.accounts import auth_headers
from loadtest.config import ConfigError, HarnessConfig, load_config
from loadtest.metrics import REFERENCE_CLASS, Recorder, Sample, quantile
from loadtest.receipt_contract import (
    MAX_HTTP_SAMPLES, MAX_INPUT_BYTES, MAX_TOTAL_FRAMES, OVERALL_TIMEOUT_SECONDS,
    ReceiptError, ReceiptManifest, canonical_uuid, keys, read_manifest,
    strict_json, timestamp, validate_run,
)
from loadtest.runner import REQUEST_TIMEOUT, Runner
from loadtest.ws_leg import (
    CLOSE_TIMEOUT_SECONDS, MAX_RECEIVE_BYTES, OPEN_TIMEOUT_SECONDS,
    READY_TIMEOUT_SECONDS, RECEIVE_QUEUE_HIGH_WATER, TargetOnlyConnect, _is_ready,
)

CLEANUP_BUDGET_SECONDS = 4.0  # Included in the 180-second wall budget.


def payload_hash(value) -> str:
    try:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != 48 or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError
        return hashlib.sha256(decoded).hexdigest()
    except (ValueError, UnicodeError):
        raise ReceiptError("payload_invalid") from None


@dataclass(frozen=True)
class Event:
    recipient: int
    message_id: str
    conversation_id: str
    sender_device_id: str
    payload_hash: str
    created_at: datetime
    arrived_at: float
    http_active: bool


@dataclass
class Attempt:
    payload_hash: str
    started_at: float
    message_id: str | None = None
    created_at: datetime | None = None
    status: str = "unconfirmed"


class ReceiptRecorder(Recorder):
    """Observe real mix samples without adding message POSTs to the old metrics."""

    def __init__(self, window_seconds: float, state: "Observation") -> None:
        super().__init__(window_seconds)
        self.state = state
        self.active = False
        self.first_mix_response = asyncio.Event()
        self.samples = 0

    def set_http_mix_active(self, active: bool) -> None:
        self.active = active
        super().set_http_mix_active(active)

    def record(self, sample: Sample) -> None:
        self.samples += 1
        if self.samples > MAX_HTTP_SAMPLES:
            self.state.fail("http_sample_overflow")
            return
        super().record(sample)
        if not self.active or sample.route_class == REFERENCE_CLASS or sample.status < 100:
            return
        if not self.state.cohort_open():
            return
        self.first_mix_response.set()
        at = self.state.runner._t0 + sample.at
        if self.state.window_start is not None and at >= self.state.window_start:
            if self.state.deadline is None or at <= self.state.deadline:
                self.state.http_window_responses += 1
                self.state.http_window_2xx += int(200 <= sample.status < 300)


class Observation:
    def __init__(self, config: HarnessConfig, manifest: ReceiptManifest, messages: int,
                 interval_seconds: float, settle_seconds: float) -> None:
        validate_run(config, manifest, messages, interval_seconds, settle_seconds)
        self.config, self.manifest = config, manifest
        self.message_count, self.interval, self.settle = messages, interval_seconds, settle_seconds
        self.runner = Runner(config, manifest.http)
        self.recorder = ReceiptRecorder(config.abort.window_seconds, self)
        self.runner.recorder = self.recorder
        self.runner.monitor = AbortMonitor(config.abort, self.recorder)
        self.ready = [asyncio.Event() for _ in manifest.recipients]
        self.connections = {}
        self.tasks = []
        self.errors: set[str] = set()
        self.finishing = False
        self.started_at = time.monotonic()
        self.window_start: float | None = None
        self.deadline: float | None = None
        self.attempts: list[Attempt] = []
        self.inflight: Attempt | None = None
        self.accepted: dict[str, Attempt] = {}
        self.pending: list[Event] = []
        self.received: set[tuple[int, str]] = set()
        self.latencies: list[float] = []
        self.send_latencies: list[float] = []
        self.counts = {name: 0 for name in (
            "frames", "duplicates", "invalid", "unmatched", "late", "receipts_while_http_active",
        )}
        self.http_window_responses = 0
        self.http_window_2xx = 0
        self.http_completed = False

    def fail(self, code: str) -> None:
        self.errors.add(code)
        self.runner.stop.set()

    def cohort_open(self) -> bool:
        return (len(self.connections) == len(self.ready)
                and all(event.is_set() for event in self.ready)
                and all(connection.state is State.OPEN for connection in self.connections.values()))

    def receive(self, recipient: int, frame: str | bytes) -> None:
        arrived_at = time.monotonic()
        self.counts["frames"] += 1
        if self.counts["frames"] > MAX_TOTAL_FRAMES:
            self.fail("receipt_frame_overflow")
            return
        try:
            if not isinstance(frame, str):
                raise ReceiptError("binary_frame")
            value = keys(strict_json(frame, limit=MAX_RECEIVE_BYTES), {
                "type", "conversation_id", "message_id", "sender_device_id", "ciphertext", "created_at",
            })
            if value["type"] != "message":
                raise ReceiptError("event_type")
            event = Event(recipient, canonical_uuid(value["message_id"]),
                          canonical_uuid(value["conversation_id"]), canonical_uuid(value["sender_device_id"]),
                          payload_hash(value["ciphertext"]), timestamp(value["created_at"]),
                          arrived_at, self.recorder.active)
        except ReceiptError:
            self.counts["invalid"] += 1
            self.fail("invalid_receipt_event")
            return
        if (event.conversation_id != self.manifest.conversation_id
                or event.sender_device_id != self.manifest.sender_device_id):
            self.counts["invalid"] += 1
            self.fail("receipt_identity_mismatch")
        elif event.message_id in self.accepted:
            self.reconcile(event, self.accepted[event.message_id])
        elif self.inflight is not None:
            # Bound is shared with all frames, so a flood cannot grow this list indefinitely.
            self.pending.append(event)
        else:
            self.counts["unmatched"] += 1
            self.fail("unmatched_receipt_event")

    def reconcile(self, event: Event, attempt: Attempt) -> None:
        if (event.message_id != attempt.message_id or event.payload_hash != attempt.payload_hash
                or event.created_at != attempt.created_at or event.arrived_at < attempt.started_at):
            self.counts["invalid"] += 1
            self.fail("receipt_binding_mismatch")
            return
        if self.deadline is not None and event.arrived_at > self.deadline:
            self.counts["late"] += 1
            return
        receipt_key = (event.recipient, event.message_id)
        if receipt_key in self.received:
            self.counts["duplicates"] += 1
            return
        self.received.add(receipt_key)
        self.latencies.append((event.arrived_at - attempt.started_at) * 1000)
        self.counts["receipts_while_http_active"] += int(event.http_active)

    def settle_pending(self, attempt: Attempt) -> None:
        pending, self.pending = self.pending, []
        for event in pending:
            if attempt.status == "accepted" and event.message_id == attempt.message_id:
                self.reconcile(event, attempt)
            else:
                self.counts["unmatched"] += 1
                self.fail("unmatched_receipt_event")

    async def receiver(self, index: int) -> None:
        connection, ready_key = None, None
        outcome, cleanup_timeout = "internal_error", False
        self.recorder.record_ws_attempt()
        started = time.monotonic()
        try:
            try:
                connection = await TargetOnlyConnect(
                    self.config.ws_url, open_timeout=OPEN_TIMEOUT_SECONDS,
                    close_timeout=CLOSE_TIMEOUT_SECONDS, max_size=MAX_RECEIVE_BYTES,
                    max_queue=RECEIVE_QUEUE_HIGH_WATER, compression=None, proxy=None,
                    additional_headers=auth_headers(self.manifest.recipients[index], "emulated"),
                )
            except TimeoutError:
                outcome = "handshake_timeout"
                self.fail("ws_handshake_timeout")
                return
            except (OSError, InvalidHandshake):
                outcome = "handshake_error"
                self.fail("ws_handshake_failed")
                return
            self.connections[index] = connection
            self.recorder.record_ws_connected((time.monotonic() - started) * 1000)
            try:
                async with asyncio.timeout(READY_TIMEOUT_SECONDS):
                    frame = await connection.recv()
            except TimeoutError:
                outcome = "ready_timeout"
                self.fail("ws_ready_timeout")
                return
            if not _is_ready(frame):
                outcome = "invalid_ready"
                self.fail("ws_ready_invalid")
                return
            if connection.state is not State.OPEN:
                outcome = "closed_before_ready"
                self.fail("ws_closed_before_ready")
                return
            ready_key = self.recorder.record_ws_ready(
                (time.monotonic() - started) * 1000, lambda: connection.state is State.OPEN)
            self.ready[index].set()
            while not self.runner.stop.is_set():
                self.receive(index, await connection.recv())
                await asyncio.sleep(0)
            outcome = "completed_hold" if self.finishing and not self.errors else "stopped"
        except ConnectionClosed:
            outcome = "closed_before_ready" if ready_key is None else "closed_during_hold"
            self.fail("ws_closed_before_finish")
        except asyncio.CancelledError:
            if not self.finishing:
                self.fail("receiver_cancelled")
            outcome = "completed_hold" if self.finishing and not self.errors else "stopped"
            raise
        except Exception:
            outcome = "internal_error"
            self.fail("ws_internal_failure")
        finally:
            if ready_key is not None:
                self.recorder.end_ws_ready(ready_key)
            if connection is not None:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        await connection.close()
                except TimeoutError:
                    cleanup_timeout = True
                    outcome = "cleanup_timeout"
                    self.fail("ws_cleanup_timeout")
                except asyncio.CancelledError:
                    # A stop can race a receiver already closing after its
                    # own failure. Finalize accounting even when cancellation
                    # interrupts that close rather than its receive loop.
                    outcome = "stopped"
                    self.fail("ws_cleanup_cancelled")
                except Exception:
                    outcome = "internal_error"
                    self.fail("ws_cleanup_failed")
                finally:
                    if connection.state is not State.CLOSED:
                        connection.transport.abort()
                    received = connection.protocol.close_rcvd
                    self.recorder.record_ws_close(received.code if received else (connection.close_code or 1006))
            self.recorder.record_ws_outcome(outcome, cleanup_timeout=cleanup_timeout)

    async def until_stop(self, awaitable) -> None:
        work = asyncio.ensure_future(awaitable)
        stop = asyncio.create_task(self.runner.stop.wait())
        try:
            done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                if self.runner.abort_violations:
                    self.errors.add("http_abort_threshold_exceeded")
                elif not self.errors:
                    self.errors.add("run_stopped")
                raise ReceiptError("run_stopped")
            await work
        finally:
            for task in (work, stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stop, return_exceptions=True)

    async def http_phase(self) -> None:
        try:
            await self.runner.run_http_phase()
            self.http_completed = not self.runner.stop.is_set()
            if len(self.attempts) < self.message_count and self.inflight is None:
                self.fail("http_mix_ended_before_workload_complete")
        except asyncio.CancelledError:
            if not self.finishing:
                self.fail("http_phase_cancelled")
            raise
        except Exception:
            self.fail("http_internal_failure")

    async def abort_watch(self) -> None:
        try:
            await self.runner._abort_watch()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.fail("abort_watch_internal_failure")

    async def send_one(self, client: httpx.AsyncClient, previous_start: float | None) -> float:
        # Pacing and concurrency are shared with the original HTTP mix. Check
        # dispatch spacing after acquiring them as queueing can change timing.
        await self.runner.pacer.global_bucket.acquire()
        async with self.runner.pacer.semaphore:
            if previous_start is not None:
                await asyncio.sleep(max(0, previous_start + self.interval - time.monotonic()))
            if self.runner.stop.is_set() or not self.cohort_open():
                raise ReceiptError("cohort_unavailable_before_send")
            if not self.recorder.active:
                raise ReceiptError("http_mix_ended_before_send")
            encoded = base64.b64encode(os.urandom(48)).decode("ascii")
            digest = payload_hash(encoded)
            if any(attempt.payload_hash == digest for attempt in self.attempts):
                raise ReceiptError("synthetic_payload_collision")
            body = {"sender_device_id": self.manifest.sender_device_id,
                    "ciphertext_b64": encoded, "message_type": "signal"}
            started = time.monotonic()
            attempt = Attempt(digest, started)
            self.attempts.append(attempt)
            self.inflight = attempt
            if self.window_start is None:
                self.window_start = started
            try:
                async with client.stream(
                    "POST", f"/conversations/{self.manifest.conversation_id}/messages",
                    json=body, headers=auth_headers(self.manifest.sender, "emulated"),
                ) as response:
                    if response.status_code != 201:
                        # Even a 5xx is not proof of rollback; only definite 4xx
                        # rejection is classified rejected, all others unknown.
                        attempt.status = "rejected" if 400 <= response.status_code < 500 else "unconfirmed"
                        raise ReceiptError("message_post_not_201")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_INPUT_BYTES:
                            raise ReceiptError("message_response_too_large")
                    value = keys(strict_json(bytes(raw)), {
                        "id", "conversation_id", "sender_device_id", "ciphertext_b64", "message_type", "created_at",
                    })
                    identifier = canonical_uuid(value["id"])
                    created = timestamp(value["created_at"])
                    if (value["conversation_id"] != self.manifest.conversation_id
                            or value["sender_device_id"] != self.manifest.sender_device_id
                            or payload_hash(value["ciphertext_b64"]) != digest
                            or value["message_type"] != "signal" or identifier in self.accepted):
                        raise ReceiptError("message_response_binding_invalid")
                    attempt.message_id, attempt.created_at, attempt.status = identifier, created, "accepted"
                    self.accepted[identifier] = attempt
            except httpx.HTTPError:
                self.fail("message_post_unconfirmed")
            except ReceiptError as error:
                self.fail(str(error))
            finally:
                self.send_latencies.append((time.monotonic() - started) * 1000)
                if len(self.attempts) == self.message_count and attempt.status == "accepted":
                    self.deadline = time.monotonic() + self.settle
                self.settle_pending(attempt)
                self.inflight = None
            if self.errors:
                raise ReceiptError("message_send_failed")
            return started

    async def producer(self) -> None:
        try:
            await self.recorder.first_mix_response.wait()
            async with httpx.AsyncClient(
                base_url=self.config.base_url, timeout=REQUEST_TIMEOUT, trust_env=False,
                follow_redirects=False, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            ) as client:
                previous = None
                for _ in range(self.message_count):
                    # Avoid holding a shared concurrency slot during the normal
                    # spacing delay; send_one repeats the check after queueing.
                    if previous is not None:
                        await asyncio.sleep(max(0, previous + self.interval - time.monotonic()))
                    previous = await self.send_one(client, previous)
            if self.deadline is None:
                raise ReceiptError("message_completion_unconfirmed")
            await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        except asyncio.CancelledError:
            if not self.finishing:
                self.fail("message_producer_cancelled")
            raise
        except ReceiptError as error:
            self.fail(str(error))
        except Exception:
            self.fail("message_internal_failure")

    async def run(self) -> dict:
        try:
            async with asyncio.timeout(OVERALL_TIMEOUT_SECONDS - CLEANUP_BUDGET_SECONDS):
                for index in range(len(self.ready)):
                    self.tasks.append(asyncio.create_task(self.receiver(index), name="receipt-receiver"))
                    if index + 1 < len(self.ready):
                        await self.until_stop(asyncio.sleep(1 / self.config.ws.connects_per_second))
                await self.until_stop(asyncio.gather(*(event.wait() for event in self.ready)))
                if not self.cohort_open():
                    raise ReceiptError("cohort_unavailable_after_ready")
                self.tasks.append(asyncio.create_task(self.abort_watch(), name="receipt-abort-watch"))
                http = asyncio.create_task(self.http_phase(), name="receipt-http-mix")
                producer = asyncio.create_task(self.producer(), name="receipt-producer")
                self.tasks.extend((http, producer))
                # Retain the configured HTTP phase; sockets keep draining after
                # the receipt deadline, so any actually observed late events
                # can be reported without satisfying missing receipts.
                await self.until_stop(asyncio.gather(http, producer))
                if not self.cohort_open():
                    raise ReceiptError("cohort_unavailable_at_finish")
        except TimeoutError:
            self.fail("overall_deadline_exceeded")
        except asyncio.CancelledError:
            # This public operation finalizes an aggregate NOT_PROVEN result
            # after bounded cleanup, including a caller's cancellation.
            self.fail("run_cancelled")
        except ReceiptError as error:
            self.fail(str(error))
        except Exception:
            self.fail("run_internal_failure")
        finally:
            self.finishing = True
            self.runner.stop.set()
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            try:
                async with asyncio.timeout(CLEANUP_BUDGET_SECONDS):
                    results = await asyncio.gather(*self.tasks, return_exceptions=True)
                    if any(isinstance(result, BaseException)
                           and not isinstance(result, asyncio.CancelledError) for result in results):
                        self.fail("owned_task_failed")
                    await self.runner.aclose()
            except (TimeoutError, asyncio.CancelledError):
                self.fail("cleanup_deadline_exceeded")
                for connection in self.connections.values():
                    if connection.state is not State.CLOSED:
                        connection.transport.abort()
            except Exception:
                self.fail("cleanup_internal_failure")
            if self.pending:
                self.counts["unmatched"] += len(self.pending)
                self.pending.clear()
                self.fail("unconfirmed_pending_receipts")
        # Match the original coordinator's final check: the periodic watch can
        # miss a threshold crossed between its final tick and HTTP completion.
        try:
            violations = self.runner.monitor.check(self.runner._now())
            if violations:
                self.runner.abort_violations = violations
                self.fail("http_abort_threshold_exceeded")
        except Exception:
            self.fail("abort_check_internal_failure")
        return self.report()

    def report(self) -> dict:
        expected = len(self.accepted) * len(self.ready)
        missing = expected - len(self.received)
        if self.runner.abort_violations:
            self.errors.add("http_abort_threshold_exceeded")
        if missing:
            self.errors.add("receipts_missing")
        if not self.accepted:
            self.errors.add("no_accepted_messages")
        if len(self.accepted) != self.message_count:
            self.errors.add("message_workload_incomplete")
        if not self.http_completed:
            self.errors.add("http_phase_incomplete")
        if not self.http_window_responses or not self.http_window_2xx:
            self.errors.add("concurrent_http_responses_not_observed")
        if not self.counts["receipts_while_http_active"]:
            self.errors.add("receipt_during_http_not_observed")
        complete = (
            not self.errors and len(self.accepted) == self.message_count and expected > 0
            and missing == 0 and all(event.is_set() for event in self.ready)
            and self.http_completed and self.http_window_responses > 0 and self.http_window_2xx > 0
            and self.counts["receipts_while_http_active"] > 0
        )
        ordered = sorted(self.latencies)
        return {
            "schema_version": 1,
            "status": "LOCAL_RECEIPTS_OBSERVED" if complete else "NOT_PROVEN",
            "errors": sorted(self.errors),
            "scope": "selected_manifest_recipient_cohort",
            "recipient_count": len(self.ready),
            "messages": {"requested": self.message_count, "attempted": len(self.attempts),
                         "accepted": len(self.accepted),
                         "rejected": sum(attempt.status == "rejected" for attempt in self.attempts),
                         "unconfirmed": sum(attempt.status == "unconfirmed" for attempt in self.attempts)},
            "receipts": {"expected": expected, "unique": len(self.received), "missing": missing, **self.counts},
            "request_start_to_arrival_ms": {
                "samples": len(ordered), "p50": round(quantile(ordered, .5), 3),
                "p95": round(quantile(ordered, .95), 3), "max": round(max(ordered, default=0), 3),
                "meaning": "client_request_dispatch_start_to_ws_frame_arrival_not_server_or_network_latency",
            },
            "http_ready_ws_overlap": {
                "window": "first_message_dispatch_through_final_response_plus_settle",
                "mix_responses": self.http_window_responses, "mix_2xx": self.http_window_2xx,
                "observed": self.http_window_responses > 0,
            },
            "receipt_during_http": {
                "unique_receipts_while_mix_active": self.counts["receipts_while_http_active"],
                "observed": self.counts["receipts_while_http_active"] > 0 and self.http_window_responses > 0,
            },
            "budgets": {
                "http_duration_seconds": self.config.duration_seconds,
                "wall_timeout_seconds": OVERALL_TIMEOUT_SECONDS,
                "shared_max_rps": self.config.caps.max_rps,
                "shared_initial_token_burst": max(2, self.config.caps.max_rps),
                "shared_max_inflight": self.config.caps.max_concurrent_requests,
                "reference_probe_additional_rps": 1, "reference_probe_additional_inflight": 1,
                "message_interval_seconds": self.interval, "settle_seconds": self.settle,
                "max_total_receipt_frames": MAX_TOTAL_FRAMES,
            },
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
            "duplicate_policy": "reported_without_satisfying_other_receipts_no_exactly_once_claim",
            "late_policy": "after_settle_deadline_only_if_observed_before_intentional_socket_finish",
            "not_proven": ["production_capacity", "durability_and_recovery", "device_decryption",
                           "server_receipt_rows", "all_conversation_members", "exactly_once_delivery"],
            "http_and_ws": self.recorder.summary(),
        }


async def run_workload(config: HarnessConfig, manifest: ReceiptManifest, *, messages: int = 1,
                       interval_seconds: float = 4, settle_seconds: float = 2) -> dict:
    return await Observation(config, manifest, messages, interval_seconds, settle_seconds).run()


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ReceiptError("arguments_invalid")


def main() -> int:
    parser = PrivateArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True, help="New private aggregate JSON file, never overwritten")
    parser.add_argument("--messages", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=4)
    parser.add_argument("--settle-seconds", type=float, default=2)
    try:
        args = parser.parse_args()
        # Bound config input too; generic validation's SystemExit diagnostic
        # can include its values, so none of it is copied to this CLI output.
        with open(args.config, "rb") as handle:
            if len(handle.read(MAX_INPUT_BYTES + 1)) > MAX_INPUT_BYTES:
                raise ReceiptError("config_too_large")
        try:
            config = load_config(args.config)
        except (ConfigError, Exception):
            raise ReceiptError("harness_config_invalid") from None
        manifest = read_manifest(args.manifest)
        validate_run(config, manifest, args.messages, args.interval_seconds, args.settle_seconds)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            result = asyncio.run(run_workload(config, manifest, messages=args.messages,
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
    print(json.dumps({"status": result["status"], "accepted_messages": result["messages"]["accepted"],
                      "unique_receipts": result["receipts"]["unique"], "missing_receipts": result["receipts"]["missing"]}))
    return 0 if result["status"] == "LOCAL_RECEIPTS_OBSERVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
