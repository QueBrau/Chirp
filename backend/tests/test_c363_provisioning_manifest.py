"""The existing harness must refuse partial or stale production-QA handoffs."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from loadtest.accounts import load_manifest


def document():
    return {
        "kind": "c363_synthetic_firebase_manifest", "status": "COMPLETE",
        "fixture_provenance": "synthetic_only", "project_id": "chirps-prod",
        "campus_id": "campus", "chapter_id": "chapter",
        "users": [{"uid": "synthetic", "id_token": "private-token",
                   "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}],
    }


@pytest.mark.parametrize("status", ["RUNNING", "QUARANTINED", "REFRESHING"])
def test_harness_refuses_incomplete_provisioning(tmp_path, status):
    value = document()
    value["status"] = status
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    with pytest.raises(SystemExit, match="not ready"):
        load_manifest(str(path), auth_mode="firebase")


def test_harness_refuses_stale_token_and_accepts_fresh_handoff(tmp_path):
    value = document()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    assert load_manifest(str(path), auth_mode="firebase").users[0].id_token == "private-token"
    value["users"][0]["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=14)).isoformat()
    path.write_text(json.dumps(value))
    with pytest.raises(SystemExit, match="fresh complete refresh"):
        load_manifest(str(path), auth_mode="firebase")


def test_legacy_local_manifest_still_loads(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"campus_id": "campus", "chapter_id": "chapter", "users": [{"uid": "local"}]}))
    assert load_manifest(str(path), auth_mode="emulated").users[0].uid == "local"
