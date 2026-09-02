"""Account manifest: the pre-provisioned identities the harness drives."""
from __future__ import annotations

import json
from dataclasses import dataclass


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
    (locally) or by the prod provisioning step (never this repo's code)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
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
