"""c437 local safety case register and synthetic tabletop tests."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
try:
    from safety_response import NOTICE_FIELDS, SafetyCaseStore, main, run_drill
finally:
    sys.path.pop(0)


def test_complete_notice_starts_48_hour_deadline_and_incomplete_does_not(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    complete, complete_notice = store.intake(
        received_at=start, elements=dict.fromkeys(NOTICE_FIELDS, True), actor="Jose"
    )
    incomplete, incomplete_notice = store.intake(
        received_at=start, elements={"contact": True}, actor="Jose"
    )
    assert complete_notice.complete
    assert incomplete_notice.missing == tuple(field for field in NOTICE_FIELDS if field != "contact")
    assert store.snapshot(complete)["deadline_at"] == "2026-10-08T12:00:00Z"
    assert store.snapshot(incomplete)["deadline_at"] is None
    store.complete_notice(incomplete, elements=dict.fromkeys(NOTICE_FIELDS, True), actor="Jose", when=start + timedelta(minutes=5))
    assert store.snapshot(incomplete)["deadline_at"] == "2026-10-08T12:05:00Z"
    store.close()


def test_evidence_and_audit_never_store_fixture_or_raw_path(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, _ = store.intake(
        received_at=datetime.now(timezone.utc),
        elements=dict.fromkeys(NOTICE_FIELDS, True),
        actor="Jose",
    )
    store.add_evidence(
        case_ref,
        external_ref="/private/unsafe/original.jpg",
        digest=hashlib.sha256(b"fixture").hexdigest(),
        media_type="synthetic-placeholder",
        actor="Jose",
        when=datetime.now(timezone.utc),
    )
    snapshot = store.snapshot(case_ref)
    assert "/private/unsafe" not in json.dumps(snapshot)
    assert "original.jpg" not in json.dumps(snapshot)
    store.close()


def test_action_schema_rejects_nested_or_unbounded_metadata(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, _ = store.intake(
        received_at=datetime.now(timezone.utc),
        elements=dict.fromkeys(NOTICE_FIELDS, True),
        actor="Jose",
    )
    with pytest.raises(ValueError):
        store.action(case_ref, event="access_grant", actor="Jose", details={"purpose": {"raw": "content"}, "scope": "fixture"}, when=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        store.action(case_ref, event="access_grant", actor="Jose", details={"purpose": "fixture", "scope": "fixture", "url": "https://example.invalid"}, when=datetime.now(timezone.utc))
    store.close()


def test_attempts_are_retried_and_duplicate_attempts_are_rejected(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, _ = store.intake(
        received_at=datetime.now(timezone.utc),
        elements=dict.fromkeys(NOTICE_FIELDS, True),
        actor="Jose",
    )
    when = datetime.now(timezone.utc)
    store.attempt(case_ref, surface="post", attempt=1, outcome="transient_failure", actor="Jose", verification_ref=None, when=when)
    store.attempt(case_ref, surface="post", attempt=2, outcome="removed", actor="Jose", verification_ref="verified", when=when + timedelta(minutes=1))
    # Exact replay is idempotent and does not create a second audit row.
    store.attempt(case_ref, surface="post", attempt=2, outcome="removed", actor="Jose", verification_ref="verified", when=when)
    with pytest.raises(ValueError):
        store.attempt(case_ref, surface="post", attempt=2, outcome="not_found", actor="Jose", verification_ref="verified", when=when)
    assert [item["attempt"] for item in store.snapshot(case_ref)["attempts"]] == [1, 2]
    store.close()


def test_drill_exercises_incomplete_case_retries_and_reappearance(tmp_path):
    report = run_drill(tmp_path / "drill.sqlite3")
    complete = report["complete"]
    assert report["notice"] is True
    assert report["missing"]
    assert complete["status"] == "reopened"
    assert [item["outcome"] for item in complete["attempts"]] == [
        "transient_failure", "removed", "transient_failure", "removed",
        "transient_failure", "removed", "none_found",
    ]
    assert {item["event"] for item in complete["audit"]} >= {
        "access_grant", "access_revoke", "reappearance", "child_safety_escalate"
    }


def _ready_case(store: SafetyCaseStore):
    case_ref, _ = store.intake(
        received_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        elements=dict.fromkeys(NOTICE_FIELDS, True), actor="Jose"
    )
    when = datetime(2026, 10, 6, 13, tzinfo=timezone.utc)
    for surface in ("post", "comment", "chirp", "media"):
        store.attempt(case_ref, surface=surface, attempt=1, outcome="removed", actor="Jose", verification_ref=f"receipt-{surface}", when=when)
    store.attempt(case_ref, surface="known_copy", attempt=1, outcome="none_found", actor="Braulio", verification_ref="copy-scan", when=when)
    return case_ref, when


def test_close_requires_current_generation_and_known_copy_proof(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, when = _ready_case(store)
    store.close_case(case_ref, actor="Jose", when=when)
    assert store.snapshot(case_ref)["status"] == "closed"
    store.close()

    store = SafetyCaseStore(tmp_path / "second.sqlite3")
    case_ref, when = _ready_case(store)
    store.action(case_ref, event="reappearance", actor="Braulio", details={"reason": "reported_reappearance"}, when=when)
    with pytest.raises(ValueError, match="every controlled surface"):
        store.close_case(case_ref, actor="Jose", when=when + timedelta(minutes=1))
    for surface in ("post", "comment", "chirp", "media", "known_copy"):
        store.attempt(case_ref, surface=surface, attempt=2, outcome="verified_absent", actor="Braulio", verification_ref=f"fresh-{surface}", when=when + timedelta(minutes=2))
    store.close_case(case_ref, actor="Braulio", when=when + timedelta(minutes=3))
    assert store.snapshot(case_ref)["status"] == "closed"
    store.close()


def test_later_failed_verification_invalidates_previous_success(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, when = _ready_case(store)
    store.attempt(case_ref, surface="media", attempt=2, outcome="transient_failure", actor="Jose", verification_ref=None, when=when)
    with pytest.raises(ValueError, match="every controlled surface"):
        store.close_case(case_ref, actor="Jose", when=when)
    store.attempt(case_ref, surface="media", attempt=3, outcome="verified_absent", actor="Braulio", verification_ref="media-now-absent", when=when)
    store.attempt(case_ref, surface="known_copy", attempt=2, outcome="transient_failure", actor="Jose", verification_ref=None, when=when)
    with pytest.raises(ValueError, match="known-copy"):
        store.close_case(case_ref, actor="Jose", when=when)
    store.attempt(case_ref, surface="known_copy", attempt=3, outcome="verified_absent", actor="Braulio", verification_ref="copies-removed-and-verified", when=when)
    store.close_case(case_ref, actor="Braulio", when=when)
    assert store.snapshot(case_ref)["status"] == "closed"
    store.close()


def test_failed_check_returns_closed_case_to_due_queue_and_attempts_increase(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, when = _ready_case(store)
    store.close_case(case_ref, actor="Jose", when=when)
    store.attempt(case_ref, surface="media", attempt=3, outcome="transient_failure", actor="Jose", verification_ref=None, when=when)
    assert store.snapshot(case_ref)["status"] == "open"
    assert case_ref in {item["case_ref"] for item in store.list_due(before=when + timedelta(days=3))}
    with pytest.raises(ValueError, match="must increase"):
        store.attempt(case_ref, surface="media", attempt=2, outcome="removed", actor="Jose", verification_ref="late-old-receipt", when=when)
    with pytest.raises(ValueError, match="every controlled surface"):
        store.close_case(case_ref, actor="Jose", when=when)
    store.close()


def test_invalid_notice_actor_and_raw_action_values_are_rejected(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, when = _ready_case(store)
    with pytest.raises(ValueError, match="boolean"):
        store.intake(received_at=when, elements={"signature": "false"}, actor="Jose")
    with pytest.raises(ValueError, match="actor"):
        store.assign(case_ref, primary="Jose", backup="Braulio", actor="arbitrary personal data", when=when)
    with pytest.raises(ValueError, match="unsupported"):
        store.action(case_ref, event="access_grant", actor="Jose", details={"purpose": "/private/person.jpg", "scope": "controlled_media"}, when=when)
    store.action(case_ref, event="appeal", actor="Jose", details={"decision_ref": "private-decision-reference"}, when=when)
    assert "private-decision-reference" not in json.dumps(store.snapshot(case_ref))
    store.close()


def test_hold_blocks_close_and_success_requires_receipt(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    case_ref, when = _ready_case(store)
    store.hold(case_ref, active=True, actor="Jose", when=when)
    with pytest.raises(ValueError, match="hold"):
        store.close_case(case_ref, actor="Jose", when=when)
    with pytest.raises(ValueError, match="verification"):
        store.attempt(case_ref, surface="post", attempt=2, outcome="not_found", actor="Jose", verification_ref=None, when=when)
    store.close()


def test_operator_cli_handles_reopen_escalation_hold_and_external_references(tmp_path, capsys):
    args = ["--db", str(tmp_path / "operator.sqlite3")]
    assert main(args + ["intake", "--elements", ",".join(NOTICE_FIELDS)]) == 0
    case_ref = json.loads(capsys.readouterr().out)["case_ref"]
    assert main(args + ["reopen", case_ref]) == 0
    assert main(args + ["escalate", case_ref]) == 0
    assert main(args + ["hold", case_ref]) == 0
    assert main(args + ["release-hold", case_ref]) == 0
    assert main(args + ["appeal", case_ref, "--reference", "external-decision-opaque"]) == 0
    assert main(args + ["report-confirmed-csam", case_ref, "--reference", "external-report-opaque"]) == 0
    assert main(args + ["status", case_ref]) == 0
    snapshot_text = capsys.readouterr().out
    result = json.loads(snapshot_text)
    assert result["status"] == "reopened"
    assert result["active_hold"] is False
    assert "external-decision-opaque" not in snapshot_text
    assert "external-report-opaque" not in snapshot_text
    with pytest.raises(ValueError, match="unrecognized notice"):
        main(args + ["intake", "--elements", "signature_typo"])
