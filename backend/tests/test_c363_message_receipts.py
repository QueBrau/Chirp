"""Real ephemeral loopback peers exercise the separate receipt instrument."""
from __future__ import annotations

import asyncio
import ast
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest
from websockets.asyncio.server import serve

from loadtest import message_receipts as workload
from loadtest.config import load_config
from loadtest.metrics import REFERENCE_CLASS, Sample
from loadtest.receipt_contract import ReceiptError, parse_manifest, validate_run, validate_target
from tests.test_c226_load_harness import _config_dict, _write


def manifest_data(recipients=2):
    users = [{"uid": f"receipt-test-{index}", "user_id": str(uuid4())} for index in range(recipients + 1)]
    return {"schema_version": 1, "campus_id": str(uuid4()), "chapter_id": str(uuid4()),
            "users": users, "message_workload": {
                "sender_uid": users[0]["uid"], "sender_device_id": str(uuid4()),
                "conversation_id": str(uuid4()), "recipient_uids": [user["uid"] for user in users[1:]],
            }}


def config_for(tmp_path, api_port=12345, ws_port=12346, **changes):
    config = load_config(_write(tmp_path, _config_dict()))
    return replace(config, base_url=f"http://127.0.0.1:{api_port}",
                   ws_url=f"ws://127.0.0.1:{ws_port}/ws", duration_seconds=.35,
                   ramp_in_seconds=0, think_seconds=.015, mix_weights={"me": 1},
                   caps=replace(config.caps, max_rps=20, max_concurrent_requests=10),
                   ws=replace(config.ws, connects_per_second=10), **changes)


class Peer:
    def __init__(self, raw, mode="normal"):
        self.raw, self.mode = raw, mode
        self.sockets = {}
        self.requests = []
        self.payloads = []
        self.post_starts = []
        self.response_finished_at = []
        self.posted = asyncio.Event()
        self.closed = 0
        self.writers = set()
        self.handlers = set()
        self.background = []
        self.release = asyncio.Event()
        self.http_port = self.ws_port = 0

    async def ws(self, socket):
        identifier = socket.request.headers["X-Debug-Firebase-Uid"]
        self.sockets[identifier] = socket
        try:
            if self.mode == "invalid_ready":
                await socket.send('{"type":"ready","type":"ready"}')
            elif self.mode != "missing_ready":
                await socket.send('{"type":"ready"}')
            if self.mode == "early_close":
                await self.posted.wait()
                await socket.close(1000)
            else:
                await socket.wait_closed()
        finally:
            self.closed += 1

    async def emit(self, result):
        event = {"type": "message", "conversation_id": result["conversation_id"],
                 "message_id": result["id"], "sender_device_id": result["sender_device_id"],
                 "ciphertext": result["ciphertext_b64"], "created_at": result["created_at"]}
        if self.mode == "wrong_id":
            event["message_id"] = str(uuid4())
        if self.mode == "wrong_device":
            event["sender_device_id"] = str(uuid4())
        if self.mode == "wrong_payload":
            event["ciphertext"] = workload.base64.b64encode(b"x" * 48).decode()
        if self.mode == "late":
            # Anchor to the collector's actual acknowledged-response deadline,
            # not a guessed server sleep versus client scheduling interval.
            while self.observation.deadline is None:
                await asyncio.sleep(.001)
            await asyncio.sleep(max(0, self.observation.deadline - time.monotonic()) + .01)
        for index, socket in enumerate(self.sockets.values()):
            if self.mode == "missing_recipient" and index == 1:
                continue
            if self.mode == "early_close":
                continue
            try:
                if self.mode == "malformed":
                    await socket.send("upstream-private-error")
                else:
                    await socket.send(json.dumps(event))
                    if self.mode == "duplicate":
                        await socket.send(json.dumps(event))
            except Exception:
                pass

    async def http(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("ascii").split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            data = await reader.readexactly(int(headers.get("Content-Length", 0)))
            self.requests.append((method, path, headers))
            status = 200
            if method == "GET" and path.endswith("/posts"):
                result = [{"id": str(uuid4())} for _ in range(5)]
            elif method == "POST" and path.endswith("/messages"):
                self.post_starts.append(time.monotonic())
                self.posted.set()
                body = json.loads(data)
                self.payloads.append(body["ciphertext_b64"])
                result = {"id": str(uuid4()), "conversation_id": self.raw["message_workload"]["conversation_id"],
                          "sender_device_id": body["sender_device_id"], "ciphertext_b64": body["ciphertext_b64"],
                          "message_type": "signal", "created_at": datetime.now(timezone.utc).isoformat()}
                if self.mode == "late":
                    self.background.append(asyncio.create_task(self.emit(result)))
                else:
                    # Deliberately deliver before completing the POST response.
                    await self.emit(result)
                await asyncio.sleep(.025)
                status = 403 if self.mode == "rejected_post" else 201
                if self.mode == "lost_response":
                    return
                if self.mode == "invalid_response":
                    result["sender_device_id"] = str(uuid4())
                if self.mode == "redirect":
                    status = 307
            else:
                if self.mode == "no_http_overlap" and self.posted.is_set():
                    await self.release.wait()
                await asyncio.sleep(.004)
                result = {"ok": True}
            body = json.dumps(result).encode()
            if method == "POST" and path.endswith("/messages"):
                self.response_finished_at.append(time.monotonic())
            extra = b"Location: https://remote.invalid/private\r\n" if status == 307 else b""
            writer.write(f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\nContent-Type: application/json\r\nConnection: close\r\n".encode()
                         + extra + b"\r\n" + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), .2)
            except (TimeoutError, ConnectionError, asyncio.CancelledError):
                writer.transport.abort()
            finally:
                self.writers.discard(writer)
                self.handlers.discard(task)


@asynccontextmanager
async def peers(raw, mode="normal"):
    peer = Peer(raw, mode)
    async with serve(peer.ws, "127.0.0.1", 0, close_timeout=.1) as ws_server:
        async with await asyncio.start_server(peer.http, "127.0.0.1", 0) as http_server:
            peer.ws_port = ws_server.sockets[0].getsockname()[1]
            peer.http_port = http_server.sockets[0].getsockname()[1]
            try:
                yield peer
            finally:
                peer.release.set()
                for writer in list(peer.writers):
                    writer.transport.abort()
                for task in list(peer.handlers):
                    task.cancel()
                for task in peer.background:
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(*peer.background, *list(peer.handlers), return_exceptions=True), 1)


async def run_peer(tmp_path, mode="normal", **options):
    raw = manifest_data()
    async with peers(raw, mode) as peer:
        config = config_for(tmp_path, peer.http_port, peer.ws_port)
        if mode == "late":
            config = replace(config, duration_seconds=1)
        peer.observation = workload.Observation(config,
                                               parse_manifest(json.dumps(raw)),
                                               options.get("messages", 1), options.get("interval_seconds", 4), .1)
        peer.arrival_bounds = []
        original_receive = peer.observation.receive
        def observed_receive(index, frame):
            before = time.monotonic()
            original_receive(index, frame)
            peer.arrival_bounds.append((before, time.monotonic()))
        peer.observation.receive = observed_receive
        result = await asyncio.wait_for(peer.observation.run(), 3)
    assert peer.closed == 2
    assert result["http_and_ws"]["ws"]["active_ready"] == 0
    assert result["http_and_ws"]["ws"]["pending"] == 0
    return result, peer, raw


@pytest.mark.asyncio
async def test_real_early_receipts_reconcile_after_http_response_and_private_aggregate(tmp_path):
    result, peer, raw = await run_peer(tmp_path)
    assert result["status"] == "LOCAL_RECEIPTS_OBSERVED"
    assert result["errors"] == []
    assert result["messages"] == {"requested": 1, "attempted": 1, "accepted": 1, "rejected": 0, "unconfirmed": 0}
    assert result["receipts"]["expected"] == result["receipts"]["unique"] == 2
    assert result["receipts"]["missing"] == 0
    assert all(0 <= latency < (peer.response_finished_at[0] - peer.observation.attempts[0].started_at) * 1000
               for latency in peer.observation.latencies)
    start = peer.observation.attempts[0].started_at
    assert len(peer.arrival_bounds) == len(peer.observation.latencies) == 2
    for latency, (before, after) in zip(peer.observation.latencies, peer.arrival_bounds):
        assert (before - start) * 1000 <= latency <= (after - start) * 1000
    assert result["http_ready_ws_overlap"]["mix_2xx"] > 0
    assert result["receipt_during_http"]["observed"]
    assert result["http_and_ws"]["http"][REFERENCE_CLASS]["count"] > 0
    assert result["budgets"]["reference_probe_additional_inflight"] == 1
    serialized = json.dumps(result)
    for user in raw["users"]:
        assert user["uid"] not in serialized and user["user_id"] not in serialized
    for value in (raw["campus_id"], raw["chapter_id"], raw["message_workload"]["sender_device_id"],
                  raw["message_workload"]["conversation_id"], *peer.payloads):
        assert value not in serialized
    assert all("Authorization" not in headers for _, _, headers in peer.requests)
    assert len(peer.post_starts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,unique,missing", [("missing_recipient", 1, 1), ("duplicate", 2, 0)])
async def test_fixed_denominator_and_duplicates_are_not_new_receipts(tmp_path, mode, unique, missing):
    result, _, _ = await run_peer(tmp_path, mode)
    assert result["receipts"]["expected"] == 2
    assert result["receipts"]["unique"] == unique and result["receipts"]["missing"] == missing
    assert result["receipts"]["duplicates"] == (2 if mode == "duplicate" else 0)
    assert result["status"] == ("LOCAL_RECEIPTS_OBSERVED" if mode == "duplicate" else "NOT_PROVEN")
    assert "exactly_once_delivery" in result["not_proven"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wrong_id", "wrong_device", "wrong_payload", "malformed", "invalid_response"])
async def test_wrong_or_malformed_identity_never_proves_receipts(tmp_path, mode):
    result, peer, _ = await run_peer(tmp_path, mode)
    assert result["status"] == "NOT_PROVEN"
    assert result["receipts"]["unique"] == 0
    assert result["receipts"]["invalid"] + result["receipts"]["unmatched"] > 0
    assert len(peer.post_starts) == 1
    assert "upstream-private-error" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rejected_post", "lost_response", "redirect"])
async def test_delivered_event_cannot_promote_failed_or_unconfirmed_post(tmp_path, mode):
    result, peer, _ = await run_peer(tmp_path, mode)
    assert result["status"] == "NOT_PROVEN"
    assert result["messages"]["accepted"] == 0
    assert result["messages"]["rejected" if mode == "rejected_post" else "unconfirmed"] == 1
    assert result["receipts"]["expected"] == result["receipts"]["unique"] == 0
    assert result["receipts"]["unmatched"] == 2
    assert len(peer.post_starts) == 1


@pytest.mark.asyncio
async def test_normal_close_before_intentional_finish_invalidates_run(tmp_path):
    result, _, _ = await run_peer(tmp_path, "early_close")
    assert result["status"] == "NOT_PROVEN"
    assert "ws_closed_before_finish" in result["errors"]
    assert result["http_and_ws"]["ws"]["close_codes"]["1000"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_ready", "missing_ready"])
async def test_full_exact_ready_fence_before_any_http_client_requests(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(workload, "READY_TIMEOUT_SECONDS", .03)
    raw = manifest_data()
    async with peers(raw, mode) as peer:
        result = await workload.run_workload(config_for(tmp_path, peer.http_port, peer.ws_port),
                                            parse_manifest(json.dumps(raw)), settle_seconds=.1)
    assert result["status"] == "NOT_PROVEN"
    assert peer.requests == []


@pytest.mark.asyncio
async def test_late_event_is_observed_but_cannot_fill_missing_receipt(tmp_path):
    result, _, _ = await run_peer(tmp_path, "late")
    assert result["status"] == "NOT_PROVEN"
    assert result["receipts"]["late"] == 2
    assert result["receipts"]["unique"] == 0 and result["receipts"]["missing"] == 2


@pytest.mark.asyncio
async def test_no_post_window_http_responses_cannot_be_hidden_by_ready_fence(tmp_path, monkeypatch):
    # Only one active mix user can finish before the POST; all later actual
    # HTTP peer responses are blocked until the run cleans up its connections.
    original = workload.Runner._user_loop
    async def one_user(self, client, user, start_delay=0):
        if user.uid != self.manifest.users[0].uid:
            await asyncio.Event().wait()
        await original(self, client, user, start_delay)
    monkeypatch.setattr(workload.Runner, "_user_loop", one_user)
    result, _, _ = await run_peer(tmp_path, "no_http_overlap")
    assert result["receipts"]["unique"] == 2
    assert result["http_and_ws"]["mixed_observation"]["observed"]
    assert result["http_ready_ws_overlap"]["mix_responses"] == 0
    assert not result["receipt_during_http"]["observed"]
    assert result["status"] == "NOT_PROVEN"


@pytest.mark.asyncio
async def test_global_bucket_and_semaphore_cover_message_dispatch_and_interval(tmp_path):
    raw = manifest_data(1)
    async with peers(raw) as peer:
        config = replace(config_for(tmp_path, peer.http_port, peer.ws_port), duration_seconds=4.5)
        observation = workload.Observation(config, parse_manifest(json.dumps(raw)), 2, 4, .1)
        original_acquire = observation.runner.pacer.global_bucket.acquire
        original_semaphore_acquire = observation.runner.pacer.semaphore.acquire
        acquisitions, slots = [], []
        async def acquire():
            acquisitions.append(asyncio.current_task().get_name())
            await original_acquire()
        async def acquire_slot():
            slots.append(asyncio.current_task().get_name())
            return await original_semaphore_acquire()
        observation.runner.pacer.global_bucket.acquire = acquire
        observation.runner.pacer.semaphore.acquire = acquire_slot
        result = await observation.run()
    assert result["status"] == "LOCAL_RECEIPTS_OBSERVED"
    assert len(peer.post_starts) == 2
    assert peer.post_starts[1] - peer.post_starts[0] >= 3.99
    assert acquisitions.count("receipt-producer") == slots.count("receipt-producer") == 2
    assert observation.runner.pacer.semaphore._value == config.caps.max_concurrent_requests


@pytest.mark.asyncio
async def test_external_cancellation_returns_incomplete_and_closes_owned_sockets(tmp_path):
    raw = manifest_data()
    async with peers(raw, "missing_ready") as peer:
        observation = workload.Observation(config_for(tmp_path, peer.http_port, peer.ws_port),
                                           parse_manifest(json.dumps(raw)), 1, 4, .1)
        task = asyncio.create_task(observation.run())
        while not peer.sockets:
            await asyncio.sleep(.005)
        task.cancel()
        result = await asyncio.wait_for(task, .5)
    assert result["status"] == "NOT_PROVEN" and "run_cancelled" in result["errors"]
    assert all(task.done() for task in observation.tasks)
    assert peer.requests == []


@pytest.mark.asyncio
async def test_frame_overflow_fails_instead_of_dropping_to_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(workload, "MAX_TOTAL_FRAMES", 1)
    result, _, _ = await run_peer(tmp_path)
    assert result["status"] == "NOT_PROVEN" and "receipt_frame_overflow" in result["errors"]
    assert result["receipts"]["unique"] <= 1


@pytest.mark.asyncio
async def test_final_abort_check_catches_violation_after_last_periodic_tick(tmp_path, monkeypatch):
    from loadtest.abort import Violation
    checks = []
    def check(self, now):
        checks.append(now)
        return [Violation("error_rate_pct", 100, 1)] if now >= .3 else []
    monkeypatch.setattr(workload.AbortMonitor, "check", check)
    result, _, _ = await run_peer(tmp_path)
    assert result["receipts"]["unique"] == 2
    assert result["status"] == "NOT_PROVEN" and "http_abort_threshold_exceeded" in result["errors"]
    assert checks


@pytest.mark.asyncio
async def test_abort_watch_internal_error_stops_writes_and_invalidates_run(tmp_path, monkeypatch):
    async def bad_watch(self):
        raise RuntimeError("private-upstream-error")
    monkeypatch.setattr(workload.Runner, "_abort_watch", bad_watch)
    result, _, _ = await run_peer(tmp_path)
    assert result["messages"]["attempted"] == 0
    assert result["status"] == "NOT_PROVEN" and "abort_watch_internal_failure" in result["errors"]
    assert "private-upstream-error" not in json.dumps(result)


@pytest.mark.asyncio
async def test_rejected_first_send_prevents_remaining_planned_messages(tmp_path):
    raw = manifest_data()
    async with peers(raw, "rejected_post") as peer:
        config = replace(config_for(tmp_path, peer.http_port, peer.ws_port), duration_seconds=4.5)
        result = await workload.run_workload(config, parse_manifest(json.dumps(raw)),
                                            messages=2, interval_seconds=4, settle_seconds=.1)
    assert result["status"] == "NOT_PROVEN"
    assert result["messages"]["requested"] == 2 and result["messages"]["attempted"] == 1
    assert len(peer.post_starts) == 1


@pytest.mark.asyncio
async def test_proxy_environment_cannot_redirect_any_target_client(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    result, _, _ = await run_peer(tmp_path)
    assert result["status"] == "LOCAL_RECEIPTS_OBSERVED"


def test_recipient_cohort_respects_configured_socket_cap(tmp_path):
    config = config_for(tmp_path)
    config = replace(config, ws=replace(config.ws, max_sockets=1))
    with pytest.raises(ReceiptError, match="local_budget_exceeded"):
        validate_run(config, parse_manifest(json.dumps(manifest_data(2))), 1, 4, .1)


@pytest.mark.asyncio
async def test_http_completion_without_real_mix_response_does_not_wait_wall_budget(tmp_path, monkeypatch):
    async def empty(self):
        pass
    monkeypatch.setattr(workload.Runner, "run_http_phase", empty)
    result, peer, _ = await asyncio.wait_for(run_peer(tmp_path), 1)
    assert result["status"] == "NOT_PROVEN"
    assert result["messages"]["attempted"] == 0 and peer.requests == []
    assert "http_mix_ended_before_workload_complete" in result["errors"]


@pytest.mark.parametrize("target", ["http://localhost:1234", "https://127.0.0.1:1234", "http://127.1:1234",
    "http://127.0.0.1", "http://127.0.0.1:1234/", "http://user@127.0.0.1:1234",
    "http://127.0.0.1:1234?x=1", "http://127.0.0.1:1234#x", "http://127.0.0.1:1234\n",
    "http://2130706433:1234", "http://127.0.0.1.remote.invalid:1234", "http://[::ffff:127.0.0.1]:1234"])
def test_literal_target_cannot_escape(target):
    with pytest.raises(ReceiptError):
        validate_target(target, websocket=False)


def test_ipv6_explicit_loopback_is_valid_and_ws_path_is_exact():
    validate_target("http://[::1]:1234", websocket=False)
    validate_target("ws://[::1]:1234/ws", websocket=True)
    for target in ("ws://127.0.0.1:1234", "ws://127.0.0.1:1234/ws/", "ws://127.0.0.1:1234/ws?token=private"):
        with pytest.raises(ReceiptError):
            validate_target(target, websocket=True)


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(schema_version=True),
    lambda raw: raw.update(token="secret"),
    lambda raw: raw["users"][0].update(id_token="secret"),
    lambda raw: raw["users"][0].update(uid="receipt-unsafe\r\nheader"),
    lambda raw: raw["users"][1].update(user_id=raw["users"][0]["user_id"]),
    lambda raw: raw["message_workload"].update(recipient_uids=[raw["users"][0]["uid"]]),
    lambda raw: raw["message_workload"].update(recipient_uids=[]),
    lambda raw: raw.update(campus_id=raw["campus_id"].upper()),
])
def test_manifest_rejects_tokens_duplicate_identities_and_malformed_values(mutation):
    raw = manifest_data()
    mutation(raw)
    with pytest.raises(ReceiptError):
        parse_manifest(json.dumps(raw))


def test_manifest_duplicate_keys_and_byte_bound():
    for raw in ('{"schema_version":1,"schema_version":1}', " " * 65537):
        with pytest.raises(ReceiptError):
            parse_manifest(raw)


@pytest.mark.parametrize("messages,interval,settle", [(0, 4, 1), (11, 4, 1), (True, 4, 1),
    (1, 3.99, 1), (1, float("nan"), 1), (1, 4, 0), (1, 4, 31), (2, 4, 1)])
def test_message_and_time_bounds(tmp_path, messages, interval, settle):
    with pytest.raises(ReceiptError):
        validate_run(config_for(tmp_path), parse_manifest(json.dumps(manifest_data())), messages, interval, settle)


@pytest.mark.asyncio
async def test_remote_targets_refused_before_any_client_even_with_approval(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("client constructed for refused target")
    monkeypatch.setattr(workload.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(workload, "TargetOnlyConnect", forbidden)
    config = replace(config_for(tmp_path), base_url="http://remote.invalid:8000",
                     auth_mode="firebase", approved_by="operator", approved_date="2026-09-15")
    with pytest.raises(ReceiptError, match="requires_literal_loopback_target"):
        await workload.run_workload(config, parse_manifest(json.dumps(manifest_data())))


def test_cli_output_reserved_exclusively_before_network_and_errors_are_fixed(tmp_path, monkeypatch):
    raw = manifest_data()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(raw))
    config_path = _write(tmp_path, _config_dict())
    config_data = _config_dict()
    config_data["base_url"] = "http://127.0.0.1:1234"
    config_data["ws_url"] = "ws://127.0.0.1:1235/ws"
    config_data["duration_seconds"] = 1
    config_data["ramp_in_seconds"] = 0
    config_data["caps"]["max_rps"] = 20
    config_data["caps"]["max_concurrent_requests"] = 10
    config_path = _write(tmp_path, config_data)
    output = tmp_path / "out.json"
    called = []
    async def fake_run(*args, **kwargs):
        called.append(True)
        return {"status": "NOT_PROVEN", "messages": {"accepted": 0}, "receipts": {"unique": 0, "missing": 0}}
    monkeypatch.setattr(workload, "run_workload", fake_run)
    monkeypatch.setattr("sys.argv", ["receipt", "--config", config_path, "--manifest", str(manifest), "--out", str(output)])
    assert workload.main() == 2 and called == [True]
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    assert workload.main() == 2 and called == [True]
    assert output.read_bytes() == original
    output.unlink()
    output.symlink_to(manifest)
    assert workload.main() == 2 and called == [True]
    assert json.loads(manifest.read_text()) == raw


def test_configerror_systemexit_is_sanitized(tmp_path, monkeypatch):
    from loadtest.config import ConfigError
    config = tmp_path / "config.yml"
    config.write_text("{}")
    printed = []
    def reject(*args, **kwargs):
        raise ConfigError("private-config-token")
    monkeypatch.setattr(workload, "load_config", reject)
    monkeypatch.setattr("builtins.print", printed.append)
    monkeypatch.setattr("sys.argv", ["receipt", "--config", str(config), "--manifest", "unused", "--out", str(tmp_path / "out")])
    assert workload.main() == 2
    assert printed == ['{"status": "refused", "reason": "harness_config_invalid"}']


def test_argument_errors_never_echo_values_or_unknown_tokens(monkeypatch):
    printed = []
    monkeypatch.setattr("builtins.print", printed.append)
    monkeypatch.setattr("sys.argv", ["receipt", "--messages", "private-token-value"])
    assert workload.main() == 2
    assert printed == ['{"status": "refused", "reason": "arguments_invalid"}']


def test_four_second_interval_stays_at_most_half_the_actual_message_limit():
    # Inspect the application constant without importing its runtime clients.
    source = Path(__file__).parents[1] / "app/core/rate_limits.py"
    definition = next(node for node in ast.parse(source.read_text()).body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "MESSAGE_SEND_LIMIT"
                              for target in node.targets))
    max_calls, window_seconds = ast.literal_eval(definition.value)
    assert 1 / 4 <= .5 * max_calls / window_seconds


@pytest.mark.asyncio
async def test_cleanup_gather_does_not_hide_an_owned_task_exception(tmp_path):
    raw = manifest_data()
    async with peers(raw) as peer:
        observation = workload.Observation(config_for(tmp_path, peer.http_port, peer.ws_port),
                                           parse_manifest(json.dumps(raw)), 1, 4, .1)
        async def broken_owned_task():
            raise RuntimeError("private-owned-task-detail")
        observation.tasks.append(asyncio.create_task(broken_owned_task()))
        result = await observation.run()
    assert result["receipts"]["unique"] == 2
    assert result["status"] == "NOT_PROVEN" and "owned_task_failed" in result["errors"]
    assert "private-owned-task-detail" not in json.dumps(result)
