from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "c354_send_stimulus.py"
SPEC = importlib.util.spec_from_file_location("c354_send_stimulus", MODULE_PATH)
assert SPEC and SPEC.loader
sender = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sender
SPEC.loader.exec_module(sender)


class _Handler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []
    redirect_sink_hits = 0
    redirect = False

    def do_GET(self):
        type(self).requests.append((self.command, self.path))
        if self.path == "/conversations" and type(self).redirect:
            self.send_response(302)
            self.send_header("Location", "/redirect-sink")
            self.end_headers()
            return
        if self.path == "/redirect-sink":
            type(self).redirect_sink_hits += 1
        body = json.dumps([{"id": "fixture-conversation", "kind": "direct", "title": "fixture"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def server():
    _Handler.requests = []
    _Handler.redirect_sink_hits = 0
    _Handler.redirect = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def test_remote_target_requires_opt_in_before_any_request(monkeypatch):
    monkeypatch.setattr(sender, "_call", lambda *args, **kwargs: pytest.fail("request made"))
    monkeypatch.setattr(sys, "argv", ["c354-send-stimulus", "--api", "https://example.invalid", "--token", "token"])
    with pytest.raises(SystemExit, match="1"):
        sender.main()


def test_malformed_and_userinfo_targets_are_rejected():
    for api in ("ftp://example.invalid", "https://user:pass@example.invalid", "https://example.invalid/a?x=1"):
        with pytest.raises(SystemExit, match="1"):
            sender._validate_api_target(api, allow_remote=True)


def test_loopback_default_mode_lists_without_writing(server, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["c354-send-stimulus", "--api", server, "--token", "token"])
    assert sender.main() == 0
    assert _Handler.requests == [("GET", "/conversations")]
    assert "conversation(s)" in capsys.readouterr().out


def test_redirect_is_refused_without_following_sink(server):
    _Handler.redirect = True
    status, payload = sender._call(server, "/conversations", "token")
    assert status == 0
    assert payload == "network_error"
    assert _Handler.redirect_sink_hits == 0


def test_network_failure_is_clean_result():
    status, payload = sender._call("http://127.0.0.1:1", "/conversations", "token")
    assert status == 0
    assert payload == "network_error"
