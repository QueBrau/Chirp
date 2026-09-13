"""Exercise c412's actual stdout boundary without relying on caplog formatting."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]


def run_logging(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)], cwd=BACKEND_DIR,
        capture_output=True, text=True, timeout=30, check=True,
    )


def test_real_stdout_classifies_warning_and_above_without_changing_info_or_volume():
    result = run_logging('''
        import logging
        from app.core.logging_config import configure_app_logging
        log = logging.getLogger("app.tests.c412")
        configure_app_logging()
        configure_app_logging()
        log.debug("debug-must-remain-disabled")
        log.info("ordinary-info")
        for level in (30, 35, 40, 45, 50, 55):
            logging.addLevelName(level, "NONSTANDARD")
            log.log(level, "event level=%s", level)
    ''')
    lines = result.stdout.splitlines()
    assert len(lines) == 7
    assert lines[0].startswith("INFO:") and lines[0].endswith("app.tests.c412 - ordinary-info")
    rows = [json.loads(line) for line in lines[1:]]
    assert [row["severity"] for row in rows] == ["WARNING", "WARNING", "ERROR", "ERROR", "CRITICAL", "CRITICAL"]
    assert [row["message"] for row in rows] == [f"event level={level}" for level in (30, 35, 40, 45, 50, 55)]
    assert all(set(row) == {"severity", "logger", "message"} and row["logger"] == "app.tests.c412" for row in rows)
    assert "debug-must-remain-disabled" not in result.stdout + result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("message,args,expected", [
    ("row %s = %s", (3, 'line one\nline two\r"quoted"\\café'), 'row 3 = line one\nline two\r"quoted"\\café'),
    ("row %(row)d", {"row": 7}, "row 7"),
    ('{"severity":"CRITICAL","analytics":true,"signal_family":"forged"}', (),
     '{"severity":"CRITICAL","analytics":true,"signal_family":"forged"}'),
])
def test_message_formatting_stays_one_line_and_json_text_cannot_inject_fields(message, args, expected):
    result = run_logging(f'''
        import logging
        from app.core.logging_config import configure_app_logging
        configure_app_logging()
        logging.getLogger("app.tests.c412").warning({message!r}, *{(args if isinstance(args, tuple) else (args,))!r})
    ''')
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"severity": "WARNING", "logger": "app.tests.c412", "message": expected}
    assert result.stderr == ""


def test_extra_fields_and_tty_color_override_never_enter_structured_output():
    result = run_logging('''
        import logging
        from app.core.logging_config import configure_app_logging
        class Private:
            def __str__(self): raise AssertionError("private-object-was-stringified")
        configure_app_logging()
        logging.getLogger("app").handlers[0].formatter.use_colors = True
        logging.getLogger("app.tests.c412").error("safe message", extra={
            "token": "private-token", "request": Private(), "color_message": "private-color",
            "analytics": True, "signal_family": "forged", "severity": "INFO",
            "message_payload": {"nested": "private-payload"},
        })
    ''')
    assert json.loads(result.stdout) == {"severity": "ERROR", "logger": "app.tests.c412", "message": "safe message"}
    assert "private-" not in result.stdout + result.stderr
    assert "\x1b" not in result.stdout
    assert result.stderr == ""


def test_exception_and_stack_remain_inside_single_message_without_mutating_record():
    result = run_logging('''
        import json, logging
        from app.core.logging_config import configure_app_logging
        configure_app_logging()
        class CheckOriginal(logging.Handler):
            def emit(self, record):
                assert record.msg == "lookup failed %s"
                assert record.args == ("fixture",)
                assert record.exc_info[0] is ValueError
                assert record.exc_text is None
                assert "message" not in record.__dict__
                assert record.stack_info is not None
        logging.getLogger().addHandler(CheckOriginal())
        try:
            raise ValueError("fixture exception")
        except ValueError:
            logging.getLogger("app.tests.c412").exception("lookup failed %s", "fixture", stack_info=True)
    ''')
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row) == {"severity", "logger", "message"}
    assert row["severity"] == "ERROR"
    assert row["message"].startswith("lookup failed fixture\nTraceback")
    assert "ValueError: fixture exception" in row["message"]
    assert "Stack (most recent call last):" in row["message"]
    assert result.stderr == ""


def test_existing_json_emitters_remain_unwrapped_and_keep_their_exact_schemas():
    result = run_logging('''
        from app.core.logging_config import configure_app_logging
        from app.core.analytics import emit
        from app.core.operational_signals import observe
        configure_app_logging()
        observe("ws_connect_capacity_rejected")
        observe("ws_connect_suspended_rejected")
        emit("pipeline_probe", probe_id="00000000-0000-0000-0000-000000000412")
    ''')
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 3
    assert rows[:2] == [{
        "schema_version": 1, "signal_family": "chirp_operational", "event": event,
        "severity": "WARNING", "observation_scope": "process", "sampled": True,
    } for event in ("ws_connect_capacity_rejected", "ws_connect_suspended_rejected")]
    assert set(rows[2]) == {"analytics", "schema_version", "event", "emission_id", "emitted_at", "severity", "probe_id"}
    assert rows[2]["analytics"] is True and rows[2]["severity"] == "INFO"
    assert rows[2]["event"] == "pipeline_probe"
    assert result.stderr == ""


def test_configure_does_not_replace_uvicorn_access_scrubbing_or_handlers():
    result = run_logging('''
        import logging, sys
        from app.core.logging_config import configure_app_logging
        from app.core.log_scrub import install_credential_log_scrub
        access = logging.getLogger("uvicorn.access")
        access.setLevel(logging.INFO)
        access.propagate = False
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("access %(message)s"))
        access.addHandler(handler)
        install_credential_log_scrub()
        filters = tuple(access.filters)
        configure_app_logging()
        configure_app_logging()
        assert access.handlers == [handler]
        assert tuple(access.filters) == filters
        access.info('GET %s HTTP/1.1', '/media/payload.signature?refresh_token=private-value&limit=1')
    ''')
    assert result.stdout.strip() == "access GET /media/[REDACTED]?refresh_token=[REDACTED]&limit=1 HTTP/1.1"
    assert result.stderr == ""


def test_protected_emitter_stream_failures_still_suppress_private_diagnostics():
    result = run_logging('''
        import logging
        from app.core.logging_config import configure_app_logging
        from app.core.analytics import emit
        from app.core.operational_signals import observe
        class Broken:
            def write(self, message): raise RuntimeError("private-stream-details")
            def flush(self): pass
        configure_app_logging()
        logging.getLogger("app").handlers[0].setStream(Broken())
        emit("pipeline_probe", probe_id="00000000-0000-0000-0000-000000000412")
        emit("private-rejected-event")
        observe("ws_connect_capacity_rejected")
        logging.getLogger("app.services.rate_limit").warning("safe fallback")
        print("operation-finished")
    ''')
    assert result.stdout.strip() == "operation-finished"
    assert result.stderr == ""
