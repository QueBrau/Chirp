"""Bounded real loopback peers falsify the deliberate reconnect instrument."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from websockets.asyncio.server import serve
from websockets.protocol import State

from loadtest import message_receipts as receipts
from loadtest import message_recovery as recovery
from loadtest.receipt_contract import ReceiptError, parse_manifest
from tests.test_c226_load_harness import _config_dict, _write
from tests.test_c363_message_receipts import config_for, manifest_data


class RecoveryPeer:
    def __init__(self, raw, mode):
        self.raw, self.mode = raw, mode
        self.sockets = {}
        self.generations = Counter()
        self.connections = []
        self.posts = []
        self.post_starts = []
        self.post_cohorts = []
        self.requests = []
        self.history_readers = []
        self.handlers, self.writers = set(), set()
        self.background = []
        self.baseline_deadline_seen = False
        self.release = asyncio.Event()
        self.history_started = asyncio.Event()
        self.http_port = self.ws_port = 0

    async def ws(self, connection):
        uid = connection.request.headers["X-Debug-Firebase-Uid"]
        self.generations[uid] += 1
        generation = self.generations[uid]
        self.sockets[uid] = connection
        self.connections.append(connection)
        try:
            if self.mode == "reconnect_invalid_ready" and generation == 2:
                await connection.send('{"type":"ready","type":"ready"}')
            elif self.mode != "initial_ready_missing":
                await connection.send('{"type":"ready"}')
            await connection.wait_closed()
        finally:
            if self.sockets.get(uid) is connection:
                self.sockets.pop(uid, None)

    async def emit(self, row, ordinal):
        event = {"type": "message", "conversation_id": row["conversation_id"],
                 "message_id": row["id"], "sender_device_id": row["sender_device_id"],
                 "ciphertext": row["ciphertext_b64"], "created_at": row["created_at"]}
        for index, connection in enumerate(list(self.sockets.values())):
            if connection.state is not State.OPEN:
                continue
            if index == 1 and ((ordinal == 1 and self.mode == "initial_receipt_missing")
                              or (ordinal == 3 and self.mode == "final_receipt_missing")):
                continue
            await connection.send(json.dumps(event))
            if ordinal == 1 and self.mode in ("early_normal_close", "abnormal_close"):
                await connection.close(1000 if self.mode == "early_normal_close" else 4503)

    async def emit_after_baseline_deadline(self, row):
        # The POST response must establish the collector's actual phase deadline
        # before choosing a late arrival; a guessed sleep would race CI timing.
        while self.observation.deadline is None:
            await asyncio.sleep(.001)
        self.baseline_deadline_seen = True
        await asyncio.sleep(max(0, self.observation.deadline - time.monotonic()) + .01)
        await self.emit(row, 1)

    def history_body(self, uid):
        rows = [dict(row) for row in reversed(self.posts)]
        # Fault only one recipient; the other must not satisfy its denominator.
        if uid != self.raw["message_workload"]["recipient_uids"][1]:
            return rows
        offline = next(row for row in rows if row["id"] == self.posts[1]["id"])
        if self.mode == "history_missing":
            rows.remove(offline)
        elif self.mode == "history_wrong_id":
            offline["id"] = str(uuid4())
        elif self.mode == "history_wrong_conversation":
            offline["conversation_id"] = str(uuid4())
        elif self.mode == "history_wrong_device":
            offline["sender_device_id"] = str(uuid4())
        elif self.mode == "history_wrong_payload":
            offline["ciphertext_b64"] = base64.b64encode(b"x" * 48).decode()
        elif self.mode == "history_wrong_timestamp":
            offline["created_at"] = "2000-01-01T00:00:00Z"
        elif self.mode == "history_wrong_type":
            offline["message_type"] = "private-upstream-value"
        elif self.mode == "history_duplicate":
            rows.append(dict(offline))
        elif self.mode == "history_object":
            return {"private-upstream-value": rows}
        elif self.mode == "history_too_many":
            return [dict(offline, id=str(uuid4())) for _ in range(recovery.MAX_HISTORY_ROWS + 1)]
        elif self.mode == "history_too_large":
            offline["ciphertext_b64"] = "private-upstream-value" * recovery.MAX_INPUT_BYTES
        return rows

    async def http(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("ascii").split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = {key.lower(): value for key, value in
                       (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            self.requests.append((method, path, headers))
            status, extra = 200, b""
            if method == "GET" and path.endswith("/posts"):
                result = [{"id": str(uuid4())} for _ in range(5)]
            elif method == "POST" and path.endswith("/messages"):
                self.post_starts.append(time.monotonic())
                self.post_cohorts.append({uid for uid, socket in self.sockets.items()
                                          if socket.state is State.OPEN})
                sent = json.loads(body)
                row = {"id": str(uuid4()), "conversation_id": self.raw["message_workload"]["conversation_id"],
                       "sender_device_id": sent["sender_device_id"], "ciphertext_b64": sent["ciphertext_b64"],
                       "message_type": "signal", "created_at": datetime.now(timezone.utc).isoformat()}
                self.posts.append(row)
                if self.mode == "baseline_late" and len(self.posts) == 1:
                    self.background.append(asyncio.create_task(self.emit_after_baseline_deadline(row)))
                else:
                    await self.emit(row, len(self.posts))
                # Exercise early-event buffering before the validating response.
                await asyncio.sleep(.01)
                status, result = 201, row
                if self.mode == "offline_post_rejected" and len(self.posts) == 2:
                    status = 403
            elif method == "GET" and "/messages?" in path:
                self.history_started.set()
                uid = headers.get("x-debug-firebase-uid")
                self.history_readers.append(uid)
                assert path.endswith(f"?limit={recovery.MAX_HISTORY_ROWS}")
                if self.mode == "history_hangs":
                    await self.release.wait()
                result = self.history_body(uid)
                if self.mode == "history_redirect":
                    status, extra = 307, b"Location: https://remote.invalid/private\r\n"
            else:
                await asyncio.sleep(.003)
                result = {"ok": True}
            encoded = json.dumps(result).encode()
            writer.write(f"HTTP/1.1 {status} Test\r\nContent-Length: {len(encoded)}\r\nContent-Type: application/json\r\nConnection: close\r\n".encode()
                         + extra + b"\r\n" + encoded)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), .2)
            except (TimeoutError, ConnectionError, asyncio.CancelledError):
                writer.transport.abort()
            self.handlers.discard(task)
            self.writers.discard(writer)


@asynccontextmanager
async def recovery_peers(raw, mode="normal"):
    peer = RecoveryPeer(raw, mode)
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
                pending = [*peer.handlers, *peer.background]
                for task in pending:
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)


def recovery_config(tmp_path, peer):
    return replace(config_for(tmp_path, peer.http_port, peer.ws_port), duration_seconds=10)


async def run_peer(tmp_path, mode="normal"):
    raw = manifest_data(2)
    async with recovery_peers(raw, mode) as peer:
        observation = recovery.RecoveryObservation(recovery_config(tmp_path, peer),
                                                   parse_manifest(json.dumps(raw)), 4, .2)
        peer.observation = observation
        result = await asyncio.wait_for(observation.run(), 16)
    assert all(connection.state is State.CLOSED for connection in peer.connections)
    assert all(task.done() for task in observation.tasks)
    assert result["http_and_ws"]["ws"]["active_ready"] == 0
    assert result["http_and_ws"]["ws"]["pending"] == 0
    return result, peer, raw


@pytest.mark.asyncio
async def test_live_offline_history_live_sequence_spacing_caps_and_private_result(tmp_path, monkeypatch):
    raw = manifest_data(2)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    async with recovery_peers(raw) as peer:
        config = recovery_config(tmp_path, peer)
        observation = recovery.RecoveryObservation(config, parse_manifest(json.dumps(raw)), 4, .2)
        acquisitions, slots = [], []
        original_acquire = observation.runner.pacer.global_bucket.acquire
        original_slot = observation.runner.pacer.semaphore.acquire
        async def acquire():
            acquisitions.append(asyncio.current_task().get_name())
            await original_acquire()
        async def slot():
            slots.append(asyncio.current_task().get_name())
            return await original_slot()
        observation.runner.pacer.global_bucket.acquire = acquire
        observation.runner.pacer.semaphore.acquire = slot
        result = await asyncio.wait_for(observation.run(), 16)
    assert result["status"] == recovery.SUCCESS
    assert result["errors"] == [] and all(result["phases"].values())
    assert result["messages"] == {"requested": 3, "attempted": 3, "accepted": 3, "rejected": 0, "unconfirmed": 0}
    for key, expected in (("live_receipts", 4), ("history_recovery", 2)):
        assert result[key]["expected"] == result[key]["unique"] == expected
        assert result[key]["missing"] == 0
    recipients = set(raw["message_workload"]["recipient_uids"])
    assert peer.post_cohorts == [recipients, set(), recipients]
    assert Counter(peer.history_readers) == Counter({uid: 1 for uid in recipients})
    assert peer.generations == Counter({uid: 2 for uid in recipients})
    assert len(result["actual_dispatch_intervals_seconds"]) == 2
    assert all(value >= 4 for value in result["actual_dispatch_intervals_seconds"])
    # All three POSTs and both recipient history reads use the same caps.
    assert acquisitions.count("receipt-producer") == slots.count("receipt-producer") == 5
    assert observation.runner.pacer.semaphore._value == config.caps.max_concurrent_requests
    assert all(result["phase_http_mix_2xx"].values())
    assert result["intentional_recipient_closes"] == 2
    assert result["offline_message_ws_observations"] == 0
    assert all(socket.state is State.CLOSED for socket in peer.connections)
    serialized = json.dumps(result)
    private = [raw["campus_id"], raw["chapter_id"], raw["message_workload"]["conversation_id"],
               raw["message_workload"]["sender_device_id"],
               *(value for user in raw["users"] for value in user.values()),
               *(row[key] for row in peer.posts for key in ("id", "ciphertext_b64"))]
    assert all(value not in serialized for value in private)
    assert all("authorization" not in headers for _, _, headers in peer.requests)
    assert {"production_capacity", "redis_failure_recovery", "phone_reconnect_recovery"} <= set(result["not_proven"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "history_missing", "history_wrong_id", "history_wrong_conversation", "history_wrong_device",
    "history_wrong_payload", "history_wrong_timestamp", "history_wrong_type", "history_duplicate",
    "history_object", "history_too_many", "history_too_large", "history_redirect",
])
async def test_each_recipient_must_recover_exact_accepted_message_from_bounded_history(tmp_path, mode):
    result, peer, _ = await run_peer(tmp_path, mode)
    assert result["status"] == "NOT_PROVEN"
    assert result["live_receipts"]["expected"] == 4
    assert result["history_recovery"]["expected"] == 2
    assert result["history_recovery"]["unique"] <= 1
    assert result["history_recovery"]["missing"] >= 1
    assert result["phases"]["recipients_offline"] is True
    assert result["phases"]["reconnected_ready"] is True
    assert result["phases"]["history_reconciled"] is False
    assert result["phases"]["final_live"] is False
    assert len(peer.post_starts) == 2
    assert "private-upstream-value" not in json.dumps(result)
    assert any("history" in error or error in {"object_shape_invalid", "json_size_or_type_invalid"}
               for error in result["errors"])


@pytest.mark.asyncio
async def test_final_live_send_refuses_partial_history_before_http_dispatch(tmp_path, monkeypatch):
    raw = manifest_data(2)
    async with recovery_peers(raw, "history_missing") as peer:
        observation = recovery.RecoveryObservation(recovery_config(tmp_path, peer),
                                                   parse_manifest(json.dumps(raw)), 4, .2)
        original_history = observation.history

        async def return_after_partial_history(client):
            # Normal history failure already stops the producer. Deliberately
            # swallow it here to exercise the independent final-send barrier.
            with pytest.raises(ReceiptError, match="^history_accepted_message_missing$"):
                await original_history(client)
            assert observation.history_seen == {0}
            assert observation.phases["history_reconciled"] is False

        monkeypatch.setattr(observation, "history", return_after_partial_history)
        result = await asyncio.wait_for(observation.run(), 16)
    assert "history_not_reconciled_before_final_send" in result["errors"]
    assert result["status"] == "NOT_PROVEN"
    assert result["phases"]["reconnected_ready"] is True
    assert result["phases"]["history_reconciled"] is False
    assert result["phases"]["final_live"] is False
    assert result["history_recovery"]["expected"] == 2
    assert result["history_recovery"]["unique"] == 1
    assert result["history_recovery"]["missing"] == 1
    assert result["messages"]["attempted"] == result["messages"]["accepted"] == len(peer.posts) == 2
    assert all(connection.state is State.CLOSED for connection in peer.connections)
    assert all(task.done() for task in observation.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,attempted,unique", [
    ("initial_receipt_missing", 1, 1), ("final_receipt_missing", 3, 3),
])
async def test_missing_live_recipient_keeps_fixed_denominator_and_stops_sequence(tmp_path, mode, attempted, unique):
    result, peer, _ = await run_peer(tmp_path, mode)
    assert result["status"] == "NOT_PROVEN"
    assert result["messages"]["attempted"] == len(peer.posts) == attempted
    assert result["live_receipts"]["expected"] == 4
    assert result["live_receipts"]["unique"] == unique
    assert result["live_receipts"]["missing"] == 4 - unique
    assert result["phases"]["final_live"] is False
    if mode == "initial_receipt_missing":
        assert result["phases"]["recipients_offline"] is False
        assert peer.history_readers == []


@pytest.mark.asyncio
async def test_initial_live_deadline_exists_before_late_receipts_and_prevents_offline_send(tmp_path):
    result, peer, _ = await run_peer(tmp_path, "baseline_late")
    assert peer.baseline_deadline_seen
    assert result["status"] == "NOT_PROVEN"
    assert result["live_receipts"]["unique"] == 0
    assert result["live_receipts"]["expected"] == result["live_receipts"]["missing"] == 4
    assert result["phases"]["initial_live"] is False
    assert result["phases"]["recipients_offline"] is False
    assert len(peer.posts) == 1 and peer.history_readers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["early_normal_close", "abnormal_close", "reconnect_invalid_ready"])
async def test_unplanned_close_and_invalid_new_ready_cannot_be_forgiven(tmp_path, mode):
    result, peer, _ = await run_peer(tmp_path, mode)
    assert result["status"] == "NOT_PROVEN"
    assert result["phases"]["history_reconciled"] is False
    assert result["phases"]["final_live"] is False
    assert len(peer.posts) <= 2
    assert peer.history_readers == []
    assert any(error.startswith("ws_") for error in result["errors"])


def test_intentional_close_hook_binds_connection_generation_phase_and_normal_local_handshake(tmp_path):
    config = replace(config_for(tmp_path), duration_seconds=10)
    observation = recovery.RecoveryObservation(config, parse_manifest(json.dumps(manifest_data(1))), 4, .2)
    def connection(sent=1000, received=1000, remote_first=False):
        return SimpleNamespace(protocol=SimpleNamespace(
            close_sent=SimpleNamespace(code=sent) if sent else None,
            close_rcvd=SimpleNamespace(code=received) if received else None,
            close_rcvd_then_sent=remote_first))
    old = connection()
    observation.phase, observation.expected_closes = "disconnecting", {0: old}
    assert observation.expected_receiver_close(0, old)
    assert not observation.expected_receiver_close(0, connection())
    assert not observation.expected_receiver_close(1, old)
    for bad in (connection(received=4503), connection(received=1006), connection(remote_first=True),
                connection(sent=None), connection(received=None)):
        observation.expected_closes = {0: bad}
        assert not observation.expected_receiver_close(0, bad)
    observation.expected_closes = {0: old}
    observation.phase = "final_live"
    assert not observation.expected_receiver_close(0, old)


@pytest.mark.asyncio
async def test_offline_rejected_send_never_reconnects_or_reduces_denominator(tmp_path):
    result, peer, _ = await run_peer(tmp_path, "offline_post_rejected")
    assert result["status"] == "NOT_PROVEN"
    assert result["messages"]["accepted"] == 1
    assert result["messages"]["rejected"] == 1
    assert result["history_recovery"]["expected"] == result["history_recovery"]["missing"] == 2
    assert peer.history_readers == []
    assert all(generation == 1 for generation in peer.generations.values())


@pytest.mark.asyncio
async def test_cancellation_during_history_is_bounded_and_cleans_both_generations(tmp_path):
    raw = manifest_data(2)
    async with recovery_peers(raw, "history_hangs") as peer:
        observation = recovery.RecoveryObservation(recovery_config(tmp_path, peer),
                                                   parse_manifest(json.dumps(raw)), 4, .2)
        task = asyncio.create_task(observation.run())
        await asyncio.wait_for(peer.history_started.wait(), 8)
        task.cancel()
        result = await asyncio.wait_for(task, 5)
    assert result["status"] == "NOT_PROVEN" and "run_cancelled" in result["errors"]
    assert result["history_recovery"]["expected"] == result["history_recovery"]["missing"] == 2
    assert all(task.done() for task in observation.tasks)
    assert all(connection.state is State.CLOSED for connection in peer.connections)


@pytest.mark.asyncio
async def test_overall_deadline_before_ready_cannot_start_http(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "OVERALL_TIMEOUT_SECONDS", receipts.CLEANUP_BUDGET_SECONDS + .2)
    result, peer, _ = await run_peer(tmp_path, "initial_ready_missing")
    assert result["status"] == "NOT_PROVEN" and "overall_deadline_exceeded" in result["errors"]
    assert peer.requests == []
    assert result["live_receipts"]["expected"] == result["live_receipts"]["missing"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"base_url": "http://remote.invalid:8000", "approved_by": "operator", "approved_date": "2026-09-18"},
    {"ws_url": "ws://127.0.0.1:1234/ws?private=secret"}, {"auth_mode": "firebase"},
    {"duration_seconds": 8},
])
async def test_public_entrypoint_refuses_invalid_scope_before_any_network(tmp_path, monkeypatch, changes):
    def forbidden(*args, **kwargs):
        pytest.fail("client constructed for refused recovery scope")
    monkeypatch.setattr(recovery.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(receipts, "TargetOnlyConnect", forbidden)
    config = replace(config_for(tmp_path), duration_seconds=10)
    config = replace(config, **changes)
    with pytest.raises(ReceiptError):
        await recovery.run_recovery(config, parse_manifest(json.dumps(manifest_data())))


def test_cli_reserves_private_output_and_refuses_overwrite_or_symlink(tmp_path, monkeypatch, capsys):
    config = _config_dict()
    config.update(base_url="http://127.0.0.1:1234", ws_url="ws://127.0.0.1:1235/ws",
                  duration_seconds=10, ramp_in_seconds=0)
    config["caps"].update(max_rps=20, max_concurrent_requests=10)
    config_path = _write(tmp_path, config)
    manifest = tmp_path / "private-manifest.json"
    manifest.write_text(json.dumps(manifest_data()))
    out = tmp_path / "new-report.json"
    called = []
    async def fake_run(*args, **kwargs):
        called.append(True)
        return {"status": "NOT_PROVEN", "messages": {"accepted": 0},
                "live_receipts": {"missing": 4}, "history_recovery": {"missing": 2}}
    monkeypatch.setattr(recovery, "run_recovery", fake_run)
    argv = ["recovery", "--config", config_path, "--manifest", str(manifest), "--out", str(out)]
    monkeypatch.setattr("sys.argv", argv)
    assert recovery.main() == 2 and called == [True]
    assert out.stat().st_mode & 0o777 == 0o600
    original = out.read_bytes()
    assert recovery.main() == 2 and called == [True] and out.read_bytes() == original
    link = tmp_path / "symlink-report.json"
    link.symlink_to(out)
    monkeypatch.setattr("sys.argv", [*argv[:-1], str(link)])
    assert recovery.main() == 2 and called == [True] and out.read_bytes() == original
    assert str(tmp_path) not in capsys.readouterr().out
