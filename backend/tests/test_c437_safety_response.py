"""c437 local safety case register and synthetic tabletop tests."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.safety_response import NOTICE_FIELDS, SafetyCaseStore, run_drill


def test_complete_notice_starts_48_hour_deadline_and_incomplete_does_not(tmp_path):
    store = SafetyCaseStore(tmp_path / "cases.sqlite3")
    start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    complete, complete_notice = store.intake(
        received_at=start, elements=dict.fromkeys(NOTICE_FIELDS, True), actor="test"
    )
    incomplete, incomplete_notice = store.intake(
        received_at=start, elements={"contact": True}, actor="test"
    )
    assert complete_notice.complete
    assert incomplete_notice.missing == tuple(field for field in NOTICE_FIELDS if field != "contact")
    assert store.snapshot(complete)["deadline_at"] == "2026-10-08T12:00:00Z"
    assert store.snapshot(incomplete)["deadline_at"] is None
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
    with pytest.raises(Exception):
        store.attempt(case_ref, surface="post", attempt=2, outcome="removed", actor="Jose", verification_ref="verified", when=when)
    assert [item["attempt"] for item in store.snapshot(case_ref)["attempts"]] == [1, 2]
    store.close()


def test_drill_exercises_incomplete_case_retries_and_reappearance(tmp_path):
    report = run_drill(tmp_path / "drill.sqlite3")
    complete = report["complete"]
    assert report["notice"] is True
    assert report["missing"]
    assert complete["status"] == "reopened"
    assert [item["outcome"] for item in complete["attempts"]] == ["transient_failure", "removed", "none_found"]
    assert {item["event"] for item in complete["audit"]} >= {
        "access_grant", "platform_remove", "verify_absent", "reappearance", "child_safety_escalate"
    }
