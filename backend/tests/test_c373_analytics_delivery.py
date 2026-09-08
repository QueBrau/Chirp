"""Exercise the actual stdout transport and fail-closed analytics boundaries."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import types
import uuid

import pytest

from app.core import analytics
from tests.conftest import BACKEND_DIR

ACTOR = uuid.UUID("00000000-0000-0000-0000-000000000373")


def test_real_stdout_is_one_unprefixed_json_event() -> None:
    script = (
        "from app.core.logging_config import configure_app_logging\n"
        "from app.core.analytics import emit\n"
        "configure_app_logging(); configure_app_logging()\n"
        f"emit('user_signed_up', user_id='{ACTOR}', account_type='greek')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND_DIR,
        capture_output=True, text=True, timeout=30, check=True,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["analytics"] is True
    assert event["event"] == "user_signed_up"
    assert event["user_id"] == str(ACTOR)
    assert result.stderr == ""


def test_real_failed_stream_cannot_dump_private_logging_diagnostics() -> None:
    script = (
        "import logging\n"
        "from app.core.logging_config import configure_app_logging\n"
        "from app.core.analytics import emit\n"
        "class Broken:\n"
        " def write(self, text): raise RuntimeError('private-stream-detail')\n"
        " def flush(self): pass\n"
        "configure_app_logging()\n"
        "logging.getLogger('app').handlers[0].setStream(Broken())\n"
        f"emit('user_signed_up', user_id='{ACTOR}', account_type='greek')\n"
        "emit('unknown-private-event-name')\n"
        "print('operation-finished')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND_DIR,
                            capture_output=True, text=True, timeout=30, check=True)
    assert result.stdout.strip() == "operation-finished"
    assert result.stderr == "", "StreamHandler.handleError must not bypass analytics redaction"


def test_operator_probe_uses_real_emitter_without_user_or_domain_fields() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.scripts.analytics_probe", "--probe-id", str(ACTOR)],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=30, check=True,
    )
    event = json.loads(result.stdout)
    assert event["event"] == "pipeline_probe"
    assert event["probe_id"] == str(ACTOR)
    assert set(event) == {"analytics", "schema_version", "event", "emission_id",
                          "emitted_at", "severity", "probe_id"}
    assert result.stderr == ""


@pytest.mark.parametrize("props", [
    {"user_id": "private-invalid-identifier", "account_type": "greek"},
    {"user_id": ACTOR, "account_type": "private-unapproved-account-type"},
    {"user_id": ACTOR},
])
def test_invalid_or_missing_properties_drop_entire_event(props, caplog) -> None:
    with caplog.at_level(logging.INFO):
        analytics.emit("user_signed_up", **props)
    assert not [r for r in caplog.records if r.name == "app.analytics"]
    assert "private-" not in caplog.text


def test_logger_and_fallback_failures_cannot_escape(monkeypatch) -> None:
    def failed(*args, **kwargs):
        raise RuntimeError("private-value-must-not-escape")

    broken = types.SimpleNamespace(info=failed, warning=failed)
    monkeypatch.setattr(analytics, "logger", broken)
    if hasattr(analytics, "diagnostics"):
        monkeypatch.setattr(analytics, "diagnostics", broken)
    analytics.emit("user_signed_up", user_id=ACTOR, account_type="greek")


def test_failure_diagnostic_does_not_include_exception_or_props(monkeypatch, caplog) -> None:
    def failed(*args, **kwargs):
        raise RuntimeError("private-value-must-not-escape")

    monkeypatch.setattr(analytics.logger, "info", failed)
    with caplog.at_level(logging.WARNING):
        analytics.emit("user_signed_up", user_id=ACTOR, account_type="greek")
    assert caplog.records
    assert "private-value-must-not-escape" not in caplog.text
    assert str(ACTOR) not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("field", [
    "body", "email", "token", "ciphertext_b64", "chirp_id", "amount_cents",
    "metadata", "analytics", "emission_id",
])
def test_non_allowlisted_fields_never_enter_event(field, caplog) -> None:
    with caplog.at_level(logging.INFO):
        analytics.emit("user_signed_up", user_id=ACTOR, account_type="greek",
                       **{field: "private-value-must-not-escape"})
    assert not [r for r in caplog.records if r.name == "app.analytics"]
    assert "private-value-must-not-escape" not in caplog.text


def test_secret_ballot_event_cannot_add_voter_identity(caplog) -> None:
    with caplog.at_level(logging.INFO):
        analytics.emit("poll_voted", poll_id=uuid.uuid4(), chapter_id=uuid.uuid4(), user_id=ACTOR)
    assert not [r for r in caplog.records if r.name == "app.analytics"]


def test_unknown_event_name_is_not_logged(caplog) -> None:
    with caplog.at_level(logging.INFO):
        analytics.emit("private-arbitrary-event-name", user_id=ACTOR)
    assert not [r for r in caplog.records if r.name == "app.analytics"]
    assert "private-arbitrary-event-name" not in caplog.text


def test_event_string_subclass_cannot_bypass_allowlist(caplog) -> None:
    class PretendKnownEvent(str):
        def __hash__(self):
            return hash("user_signed_up")

        def __eq__(self, other):
            return other == "user_signed_up"

    with caplog.at_level(logging.INFO):
        analytics.emit(PretendKnownEvent("private-unapproved-event"),
                       user_id=ACTOR, account_type="greek")
    assert not [r for r in caplog.records if r.name == "app.analytics"]
    assert "private-unapproved-event" not in caplog.text


def test_driver_uuid_and_subclass_stringification_are_safe(caplog) -> None:
    from asyncpg.pgproto.pgproto import UUID as DriverUUID

    class SensitiveStringUUID(uuid.UUID):
        def __str__(self):
            raise AssertionError("private-stringification-must-not-run")

    with caplog.at_level(logging.INFO):
        for actor in (DriverUUID(str(ACTOR)), SensitiveStringUUID(int=ACTOR.int)):
            analytics.emit("user_signed_up", user_id=actor, account_type="greek")
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.analytics"]
    assert len(events) == 2
    assert {e["user_id"] for e in events} == {str(ACTOR)}
    assert len({e["emission_id"] for e in events}) == 2
    assert all(e["schema_version"] == 1 for e in events)
