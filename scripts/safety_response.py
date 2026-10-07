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
RESPONDERS = ("Jose", "Braulio")
CONTROLLED_SURFACES = ("post", "comment", "chirp", "media")
ATTEMPT_SURFACES = CONTROLLED_SURFACES + ("known_copy",)
ATTEMPT_OUTCOMES = ("transient_failure", "removed", "not_found", "none_found", "verified_absent")
SUCCESS_OUTCOMES = ("removed", "not_found", "none_found", "verified_absent")
ACTION_VALUES = {
    "purpose": ("triage", "removal", "appeal"),
    "scope": ("case_metadata", "controlled_media", "synthetic_fixture"),
    "reason": ("synthetic_reappearance", "reported_reappearance", "operator_review"),
    "route": ("designated_safety_contact", "legal_review"),
}
SAFE_ACTIONS = {"access_grant", "access_revoke", "appeal", "reappearance", "child_safety_escalate", "report_confirmed_csam"}


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    """Serialize an aware timestamp consistently."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    """Parse the ISO timestamps emitted by this module."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


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
    if set(elements) - set(NOTICE_FIELDS) or any(type(value) is not bool for value in elements.values()):
        raise ValueError("notice elements must be named boolean flags")
    missing = tuple(name for name in NOTICE_FIELDS if not elements.get(name, False))
    return Notice(complete=not missing, missing=missing)


class SafetyCaseStore:
    """SQLite register storing references, hashes, timestamps, and audit events only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("refusing symlinked case database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = DELETE")
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
                backup_responder TEXT,
                surfaces_json TEXT NOT NULL,
                active_hold INTEGER NOT NULL DEFAULT 0,
                valid_notice_at TEXT,
                response_generation INTEGER NOT NULL DEFAULT 0,
                reopened_at TEXT
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
                verification_ref TEXT,
                response_generation INTEGER NOT NULL DEFAULT 0
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
        surfaces: tuple[str, ...] = CONTROLLED_SURFACES,
    ) -> tuple[str, Notice]:
        """Create a case and start a deadline only for a complete notice."""
        self._validate_time(received_at)
        self._validate_actor(actor)
        self._validate_responders(primary, backup)
        self._validate_surfaces(surfaces)
        notice = assess_notice(elements)
        case_ref = "SAF-" + uuid.uuid4().hex[:12].upper()
        deadline = received_at + timedelta(hours=48) if notice.complete else None
        self.db.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_ref,
                iso(received_at),
                int(notice.complete),
                json.dumps(notice.missing),
                "open" if notice.complete else "needs_information",
                iso(deadline) if deadline else None,
                primary,
                backup,
                json.dumps(surfaces),
                0,
                iso(received_at) if notice.complete else None,
                0,
                None,
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

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")

    @staticmethod
    def _validate_responders(primary: str, backup: str) -> None:
        if primary not in RESPONDERS or backup not in RESPONDERS or primary == backup:
            raise ValueError("responders must be distinct members of the named coverage pair")

    @staticmethod
    def _validate_surfaces(surfaces: tuple[str, ...]) -> None:
        if not surfaces or any(surface not in CONTROLLED_SURFACES for surface in surfaces):
            raise ValueError("surfaces must be a non-empty subset of the controlled surface list")

    def complete_notice(self, case_ref: str, *, elements: dict[str, bool], actor: str, when: datetime) -> Notice:
        """Complete an existing notice without changing its original receipt time."""
        self._validate_time(when)
        self._validate_actor(actor)
        row = self._case(case_ref)
        notice = assess_notice(elements)
        if not notice.complete:
            raise ValueError(f"notice remains incomplete: {','.join(notice.missing)}")
        if row["notice_complete"]:
            return notice
        received = parse_time(row["received_at"])
        if when < received:
            raise ValueError("valid notice cannot precede original receipt")
        deadline = when + timedelta(hours=48)
        self.db.execute("UPDATE cases SET notice_complete=1, missing_json='[]', status='open', valid_notice_at=?, deadline_at=? WHERE case_ref=?", (iso(when), iso(deadline), case_ref))
        self._audit(case_ref, "notice_completed", actor, {"notice_complete": True}, when)
        self.db.commit()
        return notice

    def _case(self, case_ref: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM cases WHERE case_ref=?", (case_ref,)).fetchone()
        if row is None:
            raise KeyError(case_ref)
        return row

    def add_evidence(self, case_ref: str, *, external_ref: str, digest: str, media_type: str, actor: str, when: datetime) -> str:
        """Record a non-content evidence reference and digest under restricted access."""
        self._validate_time(when)
        self._validate_actor(actor)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise ValueError("evidence digest must be a SHA-256 hex digest")
        if media_type not in ("synthetic-placeholder", "image-metadata", "video-metadata", "provider-receipt"):
            raise ValueError("unsupported metadata type")
        ref = opaque_ref(external_ref)
        self.db.execute(
            "INSERT INTO evidence_manifest(case_ref,evidence_ref,digest,media_type,observed_at) VALUES (?,?,?,?,?)",
            (case_ref, ref, digest, media_type, iso(when)),
        )
        self._audit(case_ref, "evidence_manifested", actor, {"evidence_ref": ref, "media_type": media_type}, when)
        self.db.commit()
        return ref

    def action(self, case_ref: str, *, event: str, actor: str, details: dict[str, Any] | None = None, when: datetime) -> None:
        """Append one strict, metadata-only lifecycle event to a case."""
        self._validate_time(when)
        self._validate_actor(actor)
        if event not in SAFE_ACTIONS:
            raise ValueError(f"unsupported safety action: {event}")
        details = self._validate_action(event, details or {})
        self._audit(case_ref, event, actor, details, when)
        if event == "reappearance":
            self.db.execute("UPDATE cases SET status='reopened', response_generation=response_generation+1, reopened_at=? WHERE case_ref=?", (iso(when), case_ref))
        self.db.commit()

    @staticmethod
    def _validate_action(event: str, details: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "access_grant": {"purpose", "scope"}, "access_revoke": {"scope"},
            "appeal": {"decision_ref"}, "reappearance": {"reason"},
            "child_safety_escalate": {"route"}, "report_confirmed_csam": {"report_ref"},
        }[event]
        if set(details) != allowed:
            raise ValueError(f"{event} requires exactly bounded metadata keys: {sorted(allowed)}")
        normalized = {}
        for key, value in details.items():
            if key in {"decision_ref", "report_ref"}:
                if not isinstance(value, str) or not 1 <= len(value) <= 256:
                    raise ValueError("reference must be a bounded nonempty identifier")
                normalized[key] = opaque_ref(value)
            elif not isinstance(value, str) or value not in ACTION_VALUES.get(key, ()):
                raise ValueError(f"unsupported value for {key}")
            else:
                normalized[key] = value
        return normalized

    @staticmethod
    def _validate_actor(actor: str) -> None:
        if actor not in RESPONDERS and actor != "drill-operator":
            raise ValueError("actor must be a named responder")

    def attempt(self, case_ref: str, *, surface: str, attempt: int, outcome: str, actor: str, verification_ref: str | None, when: datetime) -> None:
        """Record an idempotent removal/verification attempt without contacting a provider."""
        self._validate_time(when)
        if attempt < 1:
            raise ValueError("attempt must be positive")
        if surface not in ATTEMPT_SURFACES or outcome not in ATTEMPT_OUTCOMES:
            raise ValueError("invalid controlled surface or attempt outcome")
        if outcome in SUCCESS_OUTCOMES and not verification_ref:
            raise ValueError("successful attempt requires an opaque verification reference")
        self._validate_actor(actor)
        row = self._case(case_ref)
        generation = int(row["response_generation"])
        existing = self.db.execute("SELECT * FROM attempts WHERE case_ref=? AND surface=? AND attempt=?", (case_ref, surface, attempt)).fetchone()
        redacted_ref = opaque_ref(verification_ref) if verification_ref else None
        if existing:
            if existing["response_generation"] == generation and existing["outcome"] == outcome and existing["verification_ref"] == redacted_ref:
                return
            raise ValueError("conflicting duplicate attempt")
        self.db.execute("INSERT INTO attempts(case_ref,surface,attempt,occurred_at,outcome,verification_ref,response_generation) VALUES (?,?,?,?,?,?,?)", (case_ref, surface, attempt, iso(when), outcome, redacted_ref, generation))
        self._audit(case_ref, "removal_attempt", actor, {"surface": surface, "attempt": attempt, "outcome": outcome}, when)
        self.db.commit()

    def hold(self, case_ref: str, *, active: bool, actor: str, when: datetime) -> None:
        """Set a legal or child-safety preservation hold."""
        self._validate_time(when)
        self._validate_actor(actor)
        self._case(case_ref)
        self.db.execute("UPDATE cases SET active_hold=? WHERE case_ref=?", (int(active), case_ref))
        self._audit(case_ref, "hold_set" if active else "hold_released", actor, {"active": "true" if active else "false"}, when)
        self.db.commit()

    def close_case(self, case_ref: str, *, actor: str, when: datetime) -> None:
        """Close only after all declared surfaces and known-copy review are verified."""
        self._validate_time(when)
        self._validate_actor(actor)
        row = self._case(case_ref)
        if not row["notice_complete"]:
            raise ValueError("cannot close incomplete notice")
        if row["active_hold"]:
            raise ValueError("cannot close while a preservation hold is active")
        surfaces = set(json.loads(row["surfaces_json"]))
        generation = int(row["response_generation"])
        attempts = self.db.execute(
            "SELECT surface,outcome,verification_ref FROM attempts "
            "WHERE case_ref=? AND response_generation=? ORDER BY attempt,id",
            (case_ref, generation),
        ).fetchall()
        # A later failed verification invalidates an earlier success. Evidence
        # from before reappearance is excluded by response_generation above.
        latest = {item["surface"]: item for item in attempts}
        verified = {
            surface for surface, item in latest.items()
            if item["outcome"] in SUCCESS_OUTCOMES and item["verification_ref"]
        }
        if not surfaces.issubset(verified):
            raise ValueError("cannot close until every controlled surface is independently verified")
        if "known_copy" not in verified:
            raise ValueError("cannot close until known-copy review is recorded")
        self.db.execute("UPDATE cases SET status='closed' WHERE case_ref=?", (case_ref,))
        self._audit(case_ref, "case_closed", actor, {"verified_surfaces": ",".join(sorted(surfaces))}, when)
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
            "valid_notice_at": row["valid_notice_at"],
            "response_generation": row["response_generation"],
            "primary": row["primary_responder"],
            "backup": row["backup_responder"],
            "surfaces": json.loads(row["surfaces_json"]),
            "active_hold": bool(row["active_hold"]),
            "audit": [dict(item) for item in self.db.execute("SELECT event,actor,occurred_at,details_json FROM audit WHERE case_ref=? ORDER BY id", (case_ref,))],
            "attempts": [dict(item) for item in self.db.execute("SELECT surface,attempt,occurred_at,outcome,verification_ref FROM attempts WHERE case_ref=? ORDER BY id", (case_ref,))],
        }

    def assign(self, case_ref: str, *, primary: str, backup: str, actor: str, when: datetime) -> None:
        """Assign the named primary and backup without changing receipt or deadline."""
        self._validate_time(when)
        self._validate_actor(actor)
        self._validate_responders(primary, backup)
        self._case(case_ref)
        self.db.execute("UPDATE cases SET primary_responder=?, backup_responder=? WHERE case_ref=?", (primary, backup, case_ref))
        self._audit(case_ref, "assigned", actor, {"primary": primary, "backup": backup}, when)
        self.db.commit()

    def list_due(self, *, before: datetime) -> list[dict[str, Any]]:
        """List open complete cases due by a UTC timestamp."""
        self._validate_time(before)
        rows = self.db.execute("SELECT case_ref,deadline_at,status FROM cases WHERE notice_complete=1 AND status != 'closed' AND deadline_at <= ? ORDER BY deadline_at", (iso(before),)).fetchall()
        return [dict(row) for row in rows]


def run_drill(path: str | Path) -> dict[str, Any]:
    """Exercise complete/incomplete intake, retries, access-safe evidence, and reappearance."""
    start = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    store = SafetyCaseStore(path)
    try:
        complete, notice = store.intake(received_at=start, elements=dict.fromkeys(NOTICE_FIELDS, True), actor="drill-operator")
        incomplete, missing = store.intake(received_at=start, elements={"contact": True}, actor="drill-operator")
        store.add_evidence(complete, external_ref="synthetic-fixture-001", digest=hashlib.sha256(b"harmless-fixture").hexdigest(), media_type="synthetic-placeholder", actor="Jose", when=start)
        store.action(complete, event="access_grant", actor="Jose", details={"purpose": "triage", "scope": "synthetic_fixture"}, when=start)
        for surface in ("post", "comment", "chirp"):
            store.attempt(complete, surface=surface, attempt=1, outcome="transient_failure", actor="Jose", verification_ref=None, when=start + timedelta(minutes=5))
            store.attempt(complete, surface=surface, attempt=2, outcome="removed", actor="Jose", verification_ref=f"synthetic-verify-{surface}", when=start + timedelta(minutes=9))
        store.attempt(complete, surface="known_copy", attempt=1, outcome="none_found", actor="Braulio", verification_ref="synthetic-copy-scan-001", when=start + timedelta(minutes=15))
        store.action(complete, event="access_revoke", actor="Braulio", details={"scope": "synthetic_fixture"}, when=start + timedelta(minutes=20))
        store.action(complete, event="reappearance", actor="Braulio", details={"reason": "synthetic_reappearance"}, when=start + timedelta(minutes=30))
        store.action(complete, event="child_safety_escalate", actor="Jose", details={"route": "designated_safety_contact"}, when=start + timedelta(minutes=31))
        # A reappearance is intentionally reopened; close_case is exercised by tests
        # against an independently verified, non-reappeared case.
        return {"complete": store.snapshot(complete), "incomplete": store.snapshot(incomplete), "notice": notice.complete, "missing": missing.missing}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    """Run a metadata-only operator command or the isolated synthetic drill."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/private/tmp/chirp-safety-c437.sqlite3")
    parser.add_argument("command", choices=("drill", "intake", "complete-notice", "assign", "status", "list-due", "attempt", "close", "reopen", "escalate", "hold", "release-hold", "appeal", "report-confirmed-csam"))
    parser.add_argument("case_ref", nargs="?")
    parser.add_argument("--elements", default=",")
    parser.add_argument("--received-at", default=None)
    parser.add_argument("--primary", default="Jose")
    parser.add_argument("--backup", default="Braulio")
    parser.add_argument("--surfaces", default=",".join(CONTROLLED_SURFACES))
    parser.add_argument("--actor", choices=RESPONDERS, default="Jose")
    parser.add_argument("--before", default=None)
    parser.add_argument("--surface", default=None)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--outcome", default=None)
    parser.add_argument("--verification-ref", default=None)
    parser.add_argument("--reason", choices=ACTION_VALUES["reason"], default="operator_review")
    parser.add_argument("--route", choices=ACTION_VALUES["route"], default="designated_safety_contact")
    parser.add_argument("--reference", help="Opaque appeal/report reference; stored only as a digest")
    args = parser.parse_args(argv)
    if args.command == "drill":
        print(json.dumps(run_drill(args.db), indent=2, sort_keys=True))
        return 0
    if args.command not in {"intake", "list-due"} and not args.case_ref:
        parser.error("this command requires a case reference")
    store = SafetyCaseStore(args.db)
    try:
        now = parse_time(args.received_at) if args.received_at else utc_now()
        supplied_elements = {part for part in args.elements.split(",") if part}
        if supplied_elements - set(NOTICE_FIELDS):
            raise ValueError("unrecognized notice element")
        elements = {field: field in supplied_elements for field in NOTICE_FIELDS}
        if args.command == "intake":
            case_ref, notice = store.intake(received_at=now, elements=elements, actor=args.actor, primary=args.primary, backup=args.backup, surfaces=tuple(args.surfaces.split(",")))
            print(json.dumps({"case_ref": case_ref, "notice_complete": notice.complete, "missing": notice.missing}))
        elif args.command == "complete-notice":
            print(json.dumps(store.snapshot(args.case_ref))) if store.complete_notice(args.case_ref, elements=elements, actor=args.actor, when=now) else None
        elif args.command == "assign":
            store.assign(args.case_ref, primary=args.primary, backup=args.backup, actor=args.actor, when=now)
        elif args.command == "status":
            print(json.dumps(store.snapshot(args.case_ref), sort_keys=True))
        elif args.command == "list-due":
            print(json.dumps(store.list_due(before=parse_time(args.before) if args.before else now)))
        elif args.command == "attempt":
            if not args.surface or not args.outcome:
                raise ValueError("attempt requires --surface and --outcome")
            store.attempt(args.case_ref, surface=args.surface, attempt=args.attempt, outcome=args.outcome, actor=args.actor, verification_ref=args.verification_ref, when=now)
        elif args.command == "close":
            store.close_case(args.case_ref, actor=args.actor, when=now)
        elif args.command == "reopen":
            store.action(args.case_ref, event="reappearance", actor=args.actor, details={"reason": args.reason}, when=now)
        elif args.command == "escalate":
            store.action(args.case_ref, event="child_safety_escalate", actor=args.actor, details={"route": args.route}, when=now)
        elif args.command == "hold":
            store.hold(args.case_ref, active=True, actor=args.actor, when=now)
        elif args.command == "release-hold":
            store.hold(args.case_ref, active=False, actor=args.actor, when=now)
        elif args.command in {"appeal", "report-confirmed-csam"}:
            if not args.reference:
                raise ValueError("this command requires an opaque --reference")
            event, key = ("appeal", "decision_ref") if args.command == "appeal" else ("report_confirmed_csam", "report_ref")
            store.action(args.case_ref, event=event, actor=args.actor, details={key: args.reference}, when=now)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
