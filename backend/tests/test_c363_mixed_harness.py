"""Local-only harness contract proofs; never start the application or cloud calls."""
from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from loadtest.accounts import VirtualUser
from loadtest.metrics import Recorder
from loadtest.ws_leg import _one_socket


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [4503, 1000])
async def test_upgrade_then_early_close_is_failure(code):
    async def peer(ws):
        await ws.close(code)

    recorder = Recorder(30)
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await _one_socket(f"ws://127.0.0.1:{port}", VirtualUser("local"),
                          "emulated", .08, recorder, asyncio.Event())
    assert recorder.summary()["ws"]["connected"] == 1
    assert recorder.ws_failure_pct() == 100


def test_pending_attempts_are_not_failure_samples():
    recorder = Recorder(30)
    for _ in range(50):
        recorder.record_ws_attempt()
    assert recorder.ws_failure_pct() == 0


def test_cli_default_starts_both_legs_together(tmp_path, monkeypatch):
    from loadtest import __main__ as cli
    from loadtest.runner import Runner
    from tests.test_c226_load_harness import _config_dict, _write

    entered = asyncio.Event()

    async def http(self):
        await asyncio.wait_for(entered.wait(), .1)

    async def ws(*args):
        entered.set()

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"campus_id": "local", "chapter_id": "local",
                                    "users": [{"uid": "local"}]}))
    monkeypatch.setattr(Runner, "run_http_phase", http)
    monkeypatch.setattr(cli, "run_ws_phase", ws)
    monkeypatch.setattr("sys.argv", ["loadtest", "--config", _write(tmp_path, _config_dict()),
                                    "--manifest", str(manifest), "--out", str(tmp_path / "out.json")])
    assert cli.main() == 0


def config_for(tmp_path, **changes):
    from dataclasses import replace
    from loadtest.config import load_config, WsLegConfig
    from tests.test_c226_load_harness import _config_dict, _write
    config = load_config(_write(tmp_path, _config_dict()))
    return replace(config, duration_seconds=.2, ramp_in_seconds=0, think_seconds=.02,
                   mix_weights={"me": 1}, ws=WsLegConfig(1, 20, .1), **changes)


def manifest():
    from loadtest.accounts import Manifest
    return Manifest("local", "local", [VirtualUser("local")])


async def run_peer(peer, *, hold=.08, recorder=None):
    recorder = recorder or Recorder(30)
    async with serve(peer, "127.0.0.1", 0, close_timeout=.1) as server:
        port = server.sockets[0].getsockname()[1]
        await _one_socket(f"ws://127.0.0.1:{port}", VirtualUser("local"),
                          "emulated", hold, recorder, asyncio.Event())
    return recorder.summary()["ws"]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [b'{"type":"ready"}', '{}', 'null', '[]',
    '{"type":"message"}', '{"type":"ready","extra":1}',
    '{"type":"bad","type":"ready"}', '{'])
async def test_only_exact_ready_object_is_accepted(frame):
    async def peer(ws):
        await ws.send(frame)
        await ws.wait_closed()
    result = await run_peer(peer)
    assert result["connected"] == 1 and result["ready"] == 0
    assert result["outcomes"]["invalid_ready"] == 1
    assert result["failure_pct"] == 100


@pytest.mark.asyncio
async def test_ready_and_entire_hold_are_required():
    import time
    observed_hold = []
    async def peer(ws):
        await ws.send(' { "type" : "ready" } ')
        start = time.monotonic()
        await ws.wait_closed()
        observed_hold.append(time.monotonic() - start)
    result = await run_peer(peer)
    assert result["ready"] == 1
    assert result["outcomes"]["completed_hold"] == 1
    assert result["close_codes"] == {"1000": 1}
    assert result["pending"] == result["active_ready"] == 0
    assert result["failure_pct"] == 0
    assert observed_hold[0] >= .075


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1000, 4403, 4503])
async def test_close_after_ready_before_hold_deadline_fails(code):
    async def peer(ws):
        await ws.send('{"type":"ready"}')
        await asyncio.sleep(.02)
        await ws.close(code)
    result = await run_peer(peer)
    assert result["ready"] == 1
    assert result["outcomes"]["closed_during_hold"] == 1
    assert result["outcomes"]["completed_hold"] == 0
    assert result["close_codes"] == {str(code): 1}
    assert result["failure_pct"] == 100


@pytest.mark.asyncio
async def test_missing_ack_has_deadline(monkeypatch):
    from loadtest import ws_leg
    monkeypatch.setattr(ws_leg, "READY_TIMEOUT_SECONDS", .03)
    async def peer(ws):
        await ws.wait_closed()
    result = await asyncio.wait_for(run_peer(peer), .5)
    assert result["connected"] == 1 and result["ready"] == 0
    assert result["outcomes"]["ready_timeout"] == 1


@pytest.mark.asyncio
async def test_native_handshake_timeout_is_classified(monkeypatch):
    from loadtest import ws_leg
    monkeypatch.setattr(ws_leg, "OPEN_TIMEOUT_SECONDS", .03)
    released = asyncio.Event()
    async def peer(reader, writer):
        try:
            await reader.read(4096)
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            released.set()
    recorder = Recorder(30)
    async with await asyncio.start_server(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(_one_socket(f"ws://127.0.0.1:{port}", VirtualUser("local"),
                                          "emulated", .1, recorder, asyncio.Event()), .5)
        await asyncio.wait_for(released.wait(), .5)
    result = recorder.summary()["ws"]
    assert result["connected"] == 0
    assert result["outcomes"]["handshake_timeout"] == 1
    assert result["close_codes"] == {}


@pytest.mark.asyncio
async def test_unresponsive_close_is_bounded_and_not_success(monkeypatch):
    from loadtest import ws_leg
    monkeypatch.setattr(ws_leg, "CLOSE_TIMEOUT_SECONDS", .04)
    release = asyncio.Event()
    async def peer(ws):
        await ws.send('{"type":"ready"}')
        ws.transport.pause_reading()  # Actual peer never reads/ACKs the client's close.
        await release.wait()
        ws.transport.resume_reading()
    recorder = Recorder(30)
    async with serve(peer, "127.0.0.1", 0, close_timeout=.05) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            await asyncio.wait_for(_one_socket(f"ws://127.0.0.1:{port}", VirtualUser("local"),
                                              "emulated", .02, recorder, asyncio.Event()), .3)
        finally:
            release.set()
    result = recorder.summary()["ws"]
    assert result["outcomes"]["cleanup_timeout"] == 1
    assert result["cleanup_timeouts"] == 1 and result["failure_pct"] == 100
    assert result["close_codes"] == {"1006": 1}


@pytest.mark.asyncio
async def test_cancellation_is_terminal_but_not_a_failure():
    connected = asyncio.Event()
    async def peer(ws):
        connected.set()
        await ws.wait_closed()
    recorder = Recorder(30)
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(_one_socket(f"ws://127.0.0.1:{port}", VirtualUser("local"),
                                              "emulated", .1, recorder, asyncio.Event()))
        await connected.wait()
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    result = recorder.summary()["ws"]
    assert result["outcomes"]["cancelled"] == 1 and result["pending"] == 0
    assert result["settled_for_failure_rate"] == 0 and result["failure_pct"] is None


@pytest.mark.asyncio
async def test_stop_interrupts_slow_ramp_and_active_readiness(tmp_path):
    from dataclasses import replace
    from loadtest.config import WsLegConfig
    from loadtest.ws_leg import run_ws_phase
    entered = asyncio.Event()
    async def peer(ws):
        entered.set()
        await ws.wait_closed()
    recorder, stop = Recorder(30), asyncio.Event()
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = replace(config_for(tmp_path), ws_url=f"ws://127.0.0.1:{port}",
                         ws=WsLegConfig(3, .1, 30))
        task = asyncio.create_task(run_ws_phase(config, manifest(), recorder, stop))
        await entered.wait()
        stop.set()
        await asyncio.wait_for(task, .5)
    result = recorder.summary()["ws"]
    assert result["attempts"] == 1 and result["outcomes"]["stopped"] == 1
    assert result["pending"] == 0 and result["failure_pct"] is None


def test_failure_denominator_excludes_pending_stops_and_cancellations():
    from loadtest.abort import AbortMonitor
    from tests.test_c226_load_harness import _criteria
    recorder = Recorder(30)
    for _ in range(10):
        recorder.record_ws_attempt()
    recorder.record_ws_outcome("stopped")
    recorder.record_ws_outcome("cancelled")
    monitor = AbortMonitor(_criteria(min_samples=1), recorder)
    assert monitor.check(1) == []
    recorder.record_ws_outcome("closed_before_ready")
    assert [v.criterion for v in monitor.check(1)] == ["ws_failure_pct"]
    result = recorder.summary()["ws"]
    assert result["pending"] == 7 and result["settled_for_failure_rate"] == 1
    assert result["failure_pct"] == 100


def test_overlap_excludes_probe_warmup_handshake_only_and_already_closed():
    from loadtest.metrics import Sample, REFERENCE_CLASS
    recorder = Recorder(30)
    recorder.record_ws_attempt()
    recorder.record_ws_connected(1)
    recorder.set_http_mix_active(True)
    recorder.record(Sample(0, "me", 200, 1))  # Upgrade alone proves no readiness.
    is_open = True
    key = recorder.record_ws_ready(2, lambda: is_open)
    recorder.record(Sample(1, REFERENCE_CLASS, 200, 1))
    recorder.record(Sample(2, "me", 0, 1))  # No HTTP response.
    recorder.set_http_mix_active(False)
    recorder.record(Sample(3, "posts_list", 200, 1))
    recorder.set_http_mix_active(True)
    is_open = False  # Actual parser closed, receiver hasn't observed its exception yet.
    recorder.record(Sample(4, "me", 200, 1))
    assert recorder.summary()["mixed_observation"]["observed"] is False
    is_open = True
    recorder.record(Sample(5, "me", 503, 1))
    recorder.record(Sample(6, "me", 200, 1))
    recorder.end_ws_ready(key)
    recorder.record(Sample(7, "me", 200, 1))
    assert recorder.summary()["mixed_observation"] == {
        "observed": True, "http_responses_while_ready_ws_open": 2,
        "http_2xx_while_ready_ws_open": 1,
    }


@pytest.mark.asyncio
async def test_actual_http_and_ready_ws_overlap_and_longer_ws_hold_survives(tmp_path, monkeypatch):
    from dataclasses import replace
    from loadtest.__main__ import run_phases
    from loadtest.config import WsLegConfig
    from loadtest.runner import Runner
    import time
    holds, handlers = [], set()

    async def http_peer(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    async def ws_peer(ws):
        await ws.send('{"type":"ready"}')
        start = time.monotonic()
        await ws.wait_closed()
        holds.append(time.monotonic() - start)

    async def no_warmup(self, client):
        pass  # No writes or domain app needed; actual HTTP mix and probe remain executed.

    monkeypatch.setattr(Runner, "_warmup", no_warmup)
    async with await asyncio.start_server(http_peer, "127.0.0.1", 0) as http_server:
        async with serve(ws_peer, "127.0.0.1", 0) as ws_server:
            config = replace(config_for(tmp_path),
                base_url=f"http://127.0.0.1:{http_server.sockets[0].getsockname()[1]}",
                ws_url=f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}",
                ws=WsLegConfig(2, 20, .5))
            runner = Runner(config, manifest())
            await asyncio.wait_for(run_phases(runner, ["http_mix", "ws_storm"]), 3)
        if handlers:
            await asyncio.gather(*handlers)
    summary = runner.recorder.summary()
    assert summary["mixed_observation"]["http_2xx_while_ready_ws_open"] >= 1
    assert summary["ws"]["outcomes"]["completed_hold"] == 2
    assert all(value >= .48 for value in holds)
    assert summary["ws"]["pending"] == summary["ws"]["active_ready"] == 0
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("loadtest-")]


@pytest.mark.asyncio
async def test_ws_only_abort_stops_ramp_before_next_attempt(tmp_path, monkeypatch):
    from dataclasses import replace
    from loadtest.__main__ import run_phases
    from loadtest.config import WsLegConfig
    from loadtest import runner as runner_module
    from tests.test_c226_load_harness import _criteria
    monkeypatch.setattr(runner_module, "ABORT_CHECK_INTERVAL_SECONDS", .01)
    async def peer(ws):
        await ws.close(4503)
    async with serve(peer, "127.0.0.1", 0) as server:
        config = replace(config_for(tmp_path),
            ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            ws=WsLegConfig(10, 2, 1), abort=_criteria(min_samples=1))
        runner = runner_module.Runner(config, manifest())
        await asyncio.wait_for(run_phases(runner, ["ws_storm"]), .5)
    assert runner.recorder.summary()["ws"]["attempts"] == 1
    assert [v.criterion for v in runner.abort_violations] == ["ws_failure_pct"]


@pytest.mark.asyncio
async def test_final_check_catches_late_failure_before_periodic_tick(tmp_path):
    from dataclasses import replace
    from loadtest.__main__ import run_phases
    from loadtest.runner import Runner
    from tests.test_c226_load_harness import _criteria
    async def peer(ws):
        await ws.send('{"type":"ready"}')
        await asyncio.sleep(.03)
        await ws.close(1000)
    async with serve(peer, "127.0.0.1", 0) as server:
        config = replace(config_for(tmp_path),
            ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            abort=_criteria(min_samples=1))
        runner = Runner(config, manifest())
        await run_phases(runner, ["ws_storm"])
    assert runner.abort_violations[0].criterion == "ws_failure_pct"
    assert runner.recorder.summary()["ws"]["outcomes"]["closed_during_hold"] == 1


@pytest.mark.asyncio
async def test_socket_internal_exception_propagates_and_is_recorded(tmp_path, monkeypatch):
    from loadtest import ws_leg
    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic private failure marker")
    monkeypatch.setattr(ws_leg, "connect", broken)
    recorder = Recorder(30)
    with pytest.raises(ExceptionGroup):
        await ws_leg.run_ws_phase(config_for(tmp_path), manifest(), recorder, asyncio.Event())
    result = recorder.summary()["ws"]
    assert result["outcomes"]["internal_error"] == 1 and result["pending"] == 0
    assert result["failure_pct"] == 100


@pytest.mark.asyncio
async def test_http_user_exception_cancels_other_leg_and_probe(tmp_path, monkeypatch):
    from loadtest import __main__ as cli
    from loadtest.runner import Runner
    ws_started, ws_released, probe_released = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def no_warmup(self, client):
        pass
    async def broken_user(*args):
        await ws_started.wait()
        raise RuntimeError("synthetic private failure marker")
    async def probe(self):
        try:
            await asyncio.Event().wait()
        finally:
            probe_released.set()
    async def ws(*args):
        ws_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ws_released.set()
    monkeypatch.setattr(Runner, "_warmup", no_warmup)
    monkeypatch.setattr(Runner, "_user_loop", broken_user)
    monkeypatch.setattr(Runner, "_reference_probe", probe)
    monkeypatch.setattr(cli, "run_ws_phase", ws)
    runner = Runner(config_for(tmp_path), manifest())
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(cli.run_phases(runner, ["http_mix", "ws_storm"]), 1)
    assert runner.stop.is_set() and ws_released.is_set() and probe_released.is_set()
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("loadtest-")]


def test_cli_internal_error_reports_non_success_without_exception_body(tmp_path, monkeypatch, capsys):
    from loadtest import __main__ as cli
    from tests.test_c226_load_harness import _config_dict, _write
    async def broken(*args):
        raise RuntimeError("synthetic private failure marker")
    monkeypatch.setattr(cli, "run_phases", broken)
    account_file = tmp_path / "users.json"
    account_file.write_text(json.dumps({"campus_id": "local", "chapter_id": "local",
                                        "users": [{"uid": "local"}]}))
    out = tmp_path / "out.json"
    monkeypatch.setattr("sys.argv", ["loadtest", "--config", _write(tmp_path, _config_dict()),
        "--manifest", str(account_file), "--out", str(out)])
    assert cli.main() == 2
    report = json.loads(out.read_text())
    assert report["run_status"] == "internal_error"
    assert report["coverage"] == {"http_ready_ws_overlap": "NOT_OBSERVED",
                                  "receipt_delivery": "NOT_PROVEN", "production_capacity": "NOT_PROVEN"}
    assert "synthetic private failure marker" not in out.read_text() + capsys.readouterr().out


@pytest.mark.asyncio
async def test_global_stop_prevents_queued_request_from_sending(tmp_path):
    from loadtest.runner import Runner
    runner = Runner(config_for(tmp_path), manifest())
    runner.pacer.semaphore = asyncio.Semaphore(0)
    class Client:
        async def request(self, *args, **kwargs):
            pytest.fail("stopped queued request must not reach transport")
    task = asyncio.create_task(runner._request(Client(), manifest().users[0], "me", "GET", "/auth/me"))
    await asyncio.sleep(0)
    runner.stop.set()
    runner.pacer.semaphore.release()
    assert await task is None
    assert runner.recorder.summary()["http"] == {}


@pytest.mark.parametrize("field", ["duration_seconds", "ramp_in_seconds", "think_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_timing_refused(tmp_path, field, value):
    from loadtest.config import ConfigError, load_config
    from tests.test_c226_load_harness import _config_dict, _write
    data = _config_dict()
    data[field] = value
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


@pytest.mark.parametrize("field", [("ws", "hold_seconds"), ("ws", "connects_per_second"),
    ("caps", "max_rps"), ("abort", "grace_seconds"), ("abort", "window_seconds"),
    ("abort", "read_p95_ceiling_ms")])
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_rate_and_window_refused(tmp_path, field, value):
    from loadtest.config import ConfigError, load_config
    from tests.test_c226_load_harness import _config_dict, _write
    data = _config_dict()
    data[field[0]][field[1]] = value
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_invalid_mix_rejected_before_clients_or_manifest(tmp_path, monkeypatch):
    from loadtest import __main__ as cli
    from loadtest.config import ConfigError
    from tests.test_c226_load_harness import _config_dict, _write
    data = _config_dict()
    data["mix_weights"] = {"not_a_route": 1}
    monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: pytest.fail("invalid mix reached manifest"))
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: pytest.fail("invalid mix opened HTTP client"))
    monkeypatch.setattr("sys.argv", ["loadtest", "--config", _write(tmp_path, data), "--manifest", "unused"])
    with pytest.raises(ConfigError, match="unknown route"):
        cli.main()


@pytest.mark.parametrize("mode, expected", [("--http-only", ["http_mix"]), ("--ws-only", ["ws_storm"])])
def test_cli_mode_selects_only_requested_leg(tmp_path, monkeypatch, mode, expected):
    from loadtest import __main__ as cli
    from tests.test_c226_load_harness import _config_dict, _write
    called = []
    async def phases(runner, selected):
        called.extend(selected)
    monkeypatch.setattr(cli, "run_phases", phases)
    monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: manifest())
    monkeypatch.setattr("sys.argv", ["loadtest", "--config", _write(tmp_path, _config_dict()),
        "--manifest", "unused", "--out", str(tmp_path / "out.json"), mode])
    assert cli.main() == 0 and called == expected


def test_report_incomplete_outcomes_cannot_read_completed(tmp_path):
    from loadtest.abort import Violation
    from loadtest.report import build_report
    recorder = Recorder(30)
    recorder.record_ws_attempt()
    recorder.record_ws_outcome("stopped")
    kwargs = {"phases_run": ["ws_storm"], "wall_seconds": .1}
    assert build_report(config_for(tmp_path), recorder, [], **kwargs)["run_status"] == "incomplete"
    violations = [Violation("ws_failure_pct", 100, 5)]
    assert build_report(config_for(tmp_path), recorder, violations, **kwargs)["run_status"] == "aborted"
    assert build_report(config_for(tmp_path), recorder, violations,
                        run_status="internal_error", **kwargs)["run_status"] == "internal_error"


@pytest.mark.asyncio
async def test_ws_attempt_launches_remain_paced(tmp_path):
    import time
    from dataclasses import replace
    from loadtest.config import WsLegConfig
    from loadtest.ws_leg import run_ws_phase
    starts = []
    async def peer(ws):
        starts.append(time.monotonic())
        await ws.send('{"type":"ready"}')
        await ws.wait_closed()
    recorder = Recorder(30)
    async with serve(peer, "127.0.0.1", 0) as server:
        config = replace(config_for(tmp_path), ws=WsLegConfig(3, 20, .02),
            ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        await run_ws_phase(config, manifest(), recorder, asyncio.Event())
    assert len(starts) == 3 and all(b - a >= .045 for a, b in zip(starts, starts[1:]))
    assert recorder.summary()["ws"]["outcomes"]["completed_hold"] == 3


@pytest.mark.asyncio
async def test_only_zero_weight_reads_are_not_substitution_targets(tmp_path, monkeypatch):
    from dataclasses import replace
    from loadtest.runner import Runner
    config = replace(config_for(tmp_path), mix_weights={"post_create": 1, "me": 0}, think_seconds=0)
    runner = Runner(config, manifest())
    def no_write(*args):
        runner.stop.set()
        return False
    monkeypatch.setattr(runner.pacer, "write_allowed", no_write)
    await runner._user_loop(None, manifest().users[0])
    assert runner.recorder.summary()["substituted_writes"] == 1


@pytest.mark.asyncio
async def test_received_close_code_survives_stalled_transport_cleanup(monkeypatch):
    from types import SimpleNamespace
    from websockets.protocol import State
    from websockets.frames import Close
    from loadtest import ws_leg
    class Connection:
        state = State.CLOSING
        close_code = None  # Modern protocol exposes this only after CLOSED.
        protocol = SimpleNamespace(close_rcvd=Close(4503, "synthetic private reason"))
        aborted = False
        def __init__(self):
            self.transport = SimpleNamespace(abort=self.abort)
        def abort(self):
            self.aborted = True
        async def recv(self):
            return '{"type":"ready"}'
        async def close(self):
            await asyncio.Event().wait()
    connection = Connection()
    async def connect(*args, **kwargs):
        return connection
    monkeypatch.setattr(ws_leg, "connect", connect)
    monkeypatch.setattr(ws_leg, "CLOSE_TIMEOUT_SECONDS", .02)
    recorder = Recorder(30)
    await _one_socket("ws://127.0.0.1:1", VirtualUser("local"), "emulated", .01,
                      recorder, asyncio.Event())
    result = recorder.summary()["ws"]
    assert connection.aborted
    assert result["close_codes"] == {"4503": 1}
    assert result["cleanup_timeouts"] == 1
    assert "synthetic private reason" not in json.dumps(result)


@pytest.mark.asyncio
async def test_actual_redirect_never_attempts_second_target(tmp_path, monkeypatch):
    from loadtest import ws_leg
    attempts = []
    original = ws_leg.connect
    class InterceptRemote(original):
        async def open_tcp_connection(self):
            attempts.append(self.ws_uri.host)
            if self.ws_uri.host != "127.0.0.1":
                # Intercept before DNS or TCP; no remote target is ever contacted.
                raise OSError("synthetic blocked remote target")
            return await super().open_tcp_connection()
    monkeypatch.setattr(ws_leg, "connect", InterceptRemote)
    released = asyncio.Event()
    async def redirect(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            writer.write(b"HTTP/1.1 307 Temporary Redirect\r\n"
                         b"Location: ws://unapproved.invalid/ws\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            released.set()
    recorder = Recorder(30)
    async with await asyncio.start_server(redirect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await _one_socket(f"ws://127.0.0.1:{port}", VirtualUser("local", "synthetic-token"),
                          "firebase", .01, recorder, asyncio.Event())
        await asyncio.wait_for(released.wait(), .5)
    assert attempts == ["127.0.0.1"]
    assert recorder.summary()["ws"]["outcomes"]["handshake_error"] == 1


@pytest.mark.asyncio
async def test_approved_direct_target_is_not_a_redirect(tmp_path, monkeypatch):
    from loadtest import ws_leg
    from loadtest.config import load_config
    from tests.test_c226_load_harness import _config_dict, _write
    data = _config_dict()
    data.update(base_url="https://approved.invalid", ws_url="wss://approved.invalid/ws",
                auth_mode="firebase", approval={"approved_by": "fixture", "date": "2026-09-08"})
    config = load_config(_write(tmp_path, data), confirm_park_lifted=True)
    attempts = []
    class InterceptAll(ws_leg.connect):
        async def open_tcp_connection(self):
            attempts.append(self.ws_uri.host)
            raise OSError("intercept before remote DNS/TCP")
    monkeypatch.setattr(ws_leg, "connect", InterceptAll)
    await _one_socket(config.ws_url, VirtualUser("local", "synthetic-token"), "firebase", .01,
                      Recorder(30), asyncio.Event())
    assert attempts == ["approved.invalid"]
