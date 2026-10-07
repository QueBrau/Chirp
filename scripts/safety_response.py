#!/usr/bin/env python3
"""Local, privacy-minimizing safety case register and synthetic response drill."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NOTICE_FIELDS = ("signature", "identification", "location", "good_faith", "contact")
SAFE_ACTIONS = {
    "assign",
    "access_grant",
    "access_revoke",
    "platform_remove",
    "known_copy_search",
    "known_copy_remove",
    "verify_absent",
    "appeal",
    "reappearance",
    "child_safety_escalate",
    "report_confirmed_csam",
    "note",
}


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    """Serialize an aware timestamp consistently."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    """Parse the ISO timestamps emitted by this module."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def opaque_ref(value: str) -> str:
    """Hash a supplied identifier so case records contain no target or person data."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Notice:
    """Notice metadata; values are deliberately booleans, never submitted content."""

    complete: bool
    missing: tuple[str, ...]


def assess_notice(elements: dict[str, bool]) -> Notice:
    """Determine whether all required notice elements are present."""
    missing = tuple(name for name in NOTICE_FIELDS if not elements.get(name, False))
    return Notice(complete=not missing, missing=missing)


class SafetyCaseStore:
    """SQLite register storing references, hashes, timestamps, and audit events only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_ref TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                notice_complete INTEGER NOT NULL,
                missing_json TEXT NOT NULL,
                status TEXT NOT NULL,
                deadline_at TEXT,
                primary_responder TEXT,
                backup_responder TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_ref TEXT NOT NULL REFERENCES cases(case_ref),
                event TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence_manifest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_ref TEXT NOT NULL REFERENCES cases(case_ref),
                evidence_ref TEXT NOT NULL,
                digest TEXT NOT NULL,
                media_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                access_state TEXT NOT NULL DEFAULT 'restricted'
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_ref TEXT NOT NULL REFERENCES cases(case_ref),
                surface TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                verification_ref TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_attempt
                ON attempts(case_ref, surface, attempt);
            """
        )
        self.db.commit()

    def close(self) -> None:
        """Close the local database."""
        self.db.close()

    def _audit(self, case_ref: str, event: str, actor: str, details: dict[str, Any], when: datetime) -> None:
        self.db.execute(
            "INSERT INTO audit(case_ref,event,actor,occurred_at,details_json) VALUES (?,?,?,?,?)",
            (case_ref, event, actor, iso(when), json.dumps(details, sort_keys=True)),
        )

    def intake(
        self,
        *,
        received_at: datetime,
        elements: dict[str, bool],
        actor: str,
        primary: str = "Jose",
        backup: str = "Braulio",
    ) -> tuple[str, Notice]:
        """Create a case and start a deadline only for a complete notice."""
        notice = assess_notice(elements)
        case_ref = "SAF-" + uuid.uuid4().hex[:12].upper()
        deadline = received_at + timedelta(hours=48) if notice.complete else None
        self.db.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
            (
                case_ref,
                iso(received_at),
                int(notice.complete),
                json.dumps(notice.missing),
                "open" if notice.complete else "needs_information",
                iso(deadline) if deadline else None,
                primary,
                backup,
            ),
        )
        self._audit(
            case_ref,
            "intake",
            actor,
            {"notice_complete": notice.complete, "missing": notice.missing},
            received_at,
        )
        self.db.commit()
        return case_ref, notice

    def add_evidence(self, case_ref: str, *, external_ref: str, digest: str, media_type: str, actor: str, when: datetime) -> str:
        """Record a non-content evidence reference and digest under restricted access."""
        if len(digest) < 16:
            raise ValueError("evidence digest must be a non-trivial hash")
        ref = opaque_ref(external_ref)
        self.db.execute(
            "INSERT INTO evidence_manifest(case_ref,evidence_ref,digest,media_type,observed_at) VALUES (?,?,?,?,?)",
            (case_ref, ref, digest, media_type, iso(when)),
        )
        self._audit(case_ref, "evidence_manifested", actor, {"evidence_ref": ref, "media_type": media_type}, when)
        self.db.commit()
        return ref

    def action(self, case_ref: str, *, event: str, actor: str, details: dict[str, Any] | None = None, when: datetime) -> None:
        """Append an allowlisted, metadata-only event to a case."""
        if event not in SAFE_ACTIONS:
            raise ValueError(f"unsupported safety action: {event}")
        details = details or {}
        forbidden_keys = {"body", "image", "video", "content", "raw_path", "url"}
        if forbidden_keys.intersection(details):
            raise ValueError("safety audit details cannot contain content or raw evidence paths")
        self._audit(case_ref, event, actor, details, when)
        if event == "platform_remove":
            self.db.execute("UPDATE cases SET status='removal_in_progress' WHERE case_ref=?", (case_ref,))
        elif event == "verify_absent":
            self.db.execute("UPDATE cases SET status='closed' WHERE case_ref=?", (case_ref,))
        elif event == "reappearance":
            self.db.execute("UPDATE cases SET status='reopened' WHERE case_ref=?", (case_ref,))
        self.db.commit()

    def attempt(self, case_ref: str, *, surface: str, attempt: int, outcome: str, actor: str, verification_ref: str | None, when: datetime) -> None:
        """Record an idempotent removal/verification attempt without contacting a provider."""
        if attempt < 1:
            raise ValueError("attempt must be positive")
        self.db.execute(
            "INSERT INTO attempts(case_ref,surface,attempt,occurred_at,outcome,verification_ref) VALUES (?,?,?,?,?,?)",
            (case_ref, surface, attempt, iso(when), outcome, opaque_ref(verification_ref) if verification_ref else None),
        )
        self._audit(case_ref, "removal_attempt", actor, {"surface": surface, "attempt": attempt, "outcome": outcome}, when)
        self.db.commit()

    def snapshot(self, case_ref: str) -> dict[str, Any]:
        """Return a redacted case snapshot for a responder."""
        row = self.db.execute("SELECT * FROM cases WHERE case_ref=?", (case_ref,)).fetchone()
        if row is None:
            raise KeyError(case_ref)
        return {
            "case_ref": row["case_ref"],
            "received_at": row["received_at"],
            "notice_complete": bool(row["notice_complete"]),
            "missing": json.loads(row["missing_json"]),
            "status": row["status"],
            "deadline_at": row["deadline_at"],
            "primary": row["primary_responder"],
            "backup": row["backup_responder"],
            "audit": [dict(item) for item in self.db.execute("SELECT event,actor,occurred_at,details_json FROM audit WHERE case_ref=? ORDER BY id", (case_ref,))],
            "attempts": [dict(item) for item in self.db.execute("SELECT surface,attempt,occurred_at,outcome,verification_ref FROM attempts WHERE case_ref=? ORDER BY id", (case_ref,))],
        }


def run_drill(path: str | Path) -> dict[str, Any]:
    """Exercise complete/incomplete intake, retries, access-safe evidence, and reappearance."""
    start = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    store = SafetyCaseStore(path)
    try:
        complete, notice = store.intake(received_at=start, elements=dict.fromkeys(NOTICE_FIELDS, True), actor="drill-operator")
        incomplete, missing = store.intake(received_at=start, elements={"contact": True}, actor="drill-operator")
        store.add_evidence(complete, external_ref="synthetic-fixture-001", digest=hashlib.sha256(b"harmless-fixture").hexdigest(), media_type="synthetic-placeholder", actor="Jose", when=start)
        store.action(complete, event="access_grant", actor="Jose", details={"purpose": "triage", "scope": "synthetic fixture"}, when=start)
        store.attempt(complete, surface="chirp_database_soft_remove", attempt=1, outcome="transient_failure", actor="Jose", verification_ref=None, when=start + timedelta(minutes=5))
        store.attempt(complete, surface="chirp_database_soft_remove", attempt=2, outcome="removed", actor="Jose", verification_ref="synthetic-verify-001", when=start + timedelta(minutes=9))
        store.action(complete, event="platform_remove", actor="Jose", details={"surfaces": ["post", "comment", "chirp"]}, when=start + timedelta(minutes=9))
        store.attempt(complete, surface="known_identical_copy_review", attempt=1, outcome="none_found", actor="Braulio", verification_ref="synthetic-copy-scan-001", when=start + timedelta(minutes=15))
        store.action(complete, event="verify_absent", actor="Braulio", details={"known_copy_evidence": "synthetic-copy-scan-001"}, when=start + timedelta(minutes=20))
        store.action(complete, event="reappearance", actor="Braulio", details={"reason": "synthetic reappearance"}, when=start + timedelta(minutes=30))
        store.action(complete, event="child_safety_escalate", actor="Jose", details={"route": "designated safety contact; preserve metadata only"}, when=start + timedelta(minutes=31))
        return {"complete": store.snapshot(complete), "incomplete": store.snapshot(incomplete), "notice": notice.complete, "missing": missing.missing}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    """Run the local synthetic drill CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/private/tmp/chirp-safety-c437.sqlite3")
    parser.add_argument("command", choices=("drill",))
    args = parser.parse_args(argv)
    if args.command == "drill":
        print(json.dumps(run_drill(args.db), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
