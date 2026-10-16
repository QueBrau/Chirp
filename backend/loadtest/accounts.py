"""Account manifest: the pre-provisioned identities the harness drives."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class VirtualUser:
    uid: str
    # Firebase-mode runs carry a real id token per user, minted by whoever
    # provisions the accounts (a Jose/manager step, outside this harness).
    # Emulated-mode runs leave it empty and authenticate by uid header.
    id_token: str = ""


@dataclass(frozen=True)
class Manifest:
    campus_id: str
    chapter_id: str
    users: list[VirtualUser]


def load_manifest(path: str, *, auth_mode: str) -> Manifest:
    """Load and validate the users manifest produced by scripts/seed_loadtest.py
    (locally) or by the explicit production QA provisioning tool."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("kind") == "c363_synthetic_firebase_manifest":
        if (raw.get("status") != "COMPLETE" or raw.get("fixture_provenance") != "synthetic_only"
                or raw.get("project_id") != "chirps-prod" or auth_mode != "firebase"):
            raise SystemExit("synthetic provisioning manifest is not ready")
        entries = raw.get("users")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
            raise SystemExit("synthetic provisioning manifest has invalid users")
        try:
            if len({entry["uid"] for entry in entries}) != len(entries):
                raise ValueError("duplicate subject")
            required_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
            for entry in entries:
                expiry = datetime.fromisoformat(entry["expires_at"])
                if expiry.tzinfo is None or expiry < required_expiry:
                    raise ValueError("stale expiry")
        except (KeyError, TypeError, ValueError):
            raise SystemExit("synthetic tokens require a fresh complete refresh") from None
    users = [
        VirtualUser(uid=str(u["uid"]), id_token=str(u.get("id_token", "")))
        for u in raw.get("users", [])
    ]
    if not users:
        raise SystemExit(f"manifest {path} contains no users")
    if auth_mode == "firebase":
        missing = [u.uid for u in users if not u.id_token]
        if missing:
            raise SystemExit(
                f"manifest {path}: {len(missing)} users lack id_token but auth_mode "
                f"is firebase (first: {missing[0]})"
            )
    if not raw.get("campus_id") or not raw.get("chapter_id"):
        raise SystemExit(f"manifest {path} must carry campus_id and chapter_id")
    return Manifest(
        campus_id=str(raw["campus_id"]),
        chapter_id=str(raw["chapter_id"]),
        users=users,
    )


def auth_headers(user: VirtualUser, auth_mode: str) -> dict[str, str]:
    if auth_mode == "emulated":
        return {"X-Debug-Firebase-Uid": user.uid}
    return {"Authorization": f"Bearer {user.id_token}"}
