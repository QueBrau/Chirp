from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import pytest

from loadtest import provisioning


D = "a" * 64
NOW = datetime(2026, 10, 17, 5, 0, tzinfo=timezone.utc)
OWNERSHIP = {"campus_id": "12345678-1234-4567-8123-123456789abc", "chapter_id": "12345678-1234-4567-8123-123456789abd"}


def plan(count: int = 3) -> provisioning.Plan:
    return provisioning.make_plan(
        run_id="12345678-1234-4567-8123-123456789abc",
        user_count=count,
        campus_slug="c363-oct17",
        chapter_slug="alpha",
        source_digest=D,
        image_digest="b" * 64,
        config_digest="c" * 64,
        now=NOW,
    )


def test_plan_is_deterministic_and_caps_staged_users() -> None:
    first = plan(200)
    second = plan(200)
    assert first == second
    assert len(provisioning.users_for_plan(first)) == 200
    assert provisioning.users_for_plan(first)[0].uid.startswith("c363-")
    with pytest.raises(provisioning.ProvisioningError, match="cap"):
        plan(201)


@pytest.mark.parametrize("slug", ["load-old", "UPPER", "bad_slug", "x" * 51])
def test_plan_rejects_reused_or_invalid_namespace(slug: str) -> None:
    with pytest.raises(provisioning.ProvisioningError):
        provisioning.make_plan(
            run_id=str(uuid.uuid4()), user_count=1, campus_slug=slug,
            chapter_slug="alpha", source_digest=D, image_digest=D, config_digest=D,
        )


def test_plan_digest_and_private_atomic_output(tmp_path: Path) -> None:
    p = plan(1)
    out = tmp_path / "plan.json"
    provisioning._atomic_private_json(out, provisioning.plan_document(p))
    assert out.stat().st_mode & 0o777 == 0o600
    assert json.loads(out.read_text())["plan_digest"] == p.plan_digest
    with pytest.raises(FileExistsError):
        provisioning._atomic_private_json(out, provisioning.plan_document(p))


def test_plan_cli_is_default_no_execution_and_no_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "plan.json"
    args = ["--run-id", str(uuid.uuid4()), "--users", "2", "--campus-slug", "oct17",
            "--chapter-slug", "alpha", "--source-digest", D, "--image-digest", D,
            "--config-digest", D, "--plan", str(out)]
    assert provisioning.main(args) == 0
    printed = capsys.readouterr().out
    assert "PLAN_ONLY" in printed
    assert "idToken" not in printed and "password" not in printed
    assert out.exists()


def test_execute_loads_existing_plan_and_requires_private_inputs(tmp_path: Path,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    p = plan(1)
    plan_path = tmp_path / "plan.json"
    provisioning._atomic_private_json(plan_path, provisioning.plan_document(p))
    assert provisioning.main([
        "--run-id", str(uuid.uuid4()), "--users", "200", "--campus-slug", "wrong",
        "--chapter-slug", "wrong", "--source-digest", D, "--image-digest", D,
        "--config-digest", D, "--plan", str(plan_path), "--execute",
        "--confirm-plan-digest", p.plan_digest,
    ]) == 2
    output = capsys.readouterr().out
    assert "execution_inputs_required" in output
    assert "wrong" not in output


def test_real_execution_requires_durable_manifest(tmp_path: Path) -> None:
    p = plan(1)
    with pytest.raises(provisioning.ProvisioningError, match="manifest_required"):
        provisioning.provision_users(
            plan=p, firebase=FakeFirebase(), token_client=lambda **_: {},
            token_verifier=lambda _: {}, bootstrap=lambda **_: {},
            verify_ownership=lambda **_: OWNERSHIP, now=NOW,
        )


@dataclass
class Record:
    uid: str


class FakeFirebase:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_user(self, **kwargs):
        self.calls.append(kwargs)
        return Record(kwargs["uid"])


def test_execute_binds_firebase_bootstrap_uid_and_expiry(tmp_path: Path) -> None:
    firebase = FakeFirebase()
    p = plan(1)
    user = provisioning.users_for_plan(p)[0]

    def token_client(*, email: str, password: str):
        assert email == user.email and password
        return {"idToken": "secret-token", "localId": user.uid}

    def verify(token: str):
        assert token == "secret-token"
        return {"uid": user.uid, "aud": "chirps-prod", "exp": int(NOW.timestamp()) + 3600}

    def bootstrap(*, token: str, email: str, display_name: str):
        assert token == "secret-token"
        return {"id": str(uuid.uuid4()), "firebase_uid": user.uid}

    result = provisioning.provision_users(plan=p, firebase=firebase,
                                          token_client=token_client,
                                          token_verifier=verify, bootstrap=bootstrap,
                                          verify_ownership=lambda **kwargs: OWNERSHIP,
                                          manifest_path=tmp_path / "manifest.json", now=NOW)
    assert result["fixture_provenance"] == "synthetic_only"
    assert result["users"][0]["uid"] == user.uid
    assert result["users"][0]["expires_at"].startswith("2026-10-17T06")
    assert firebase.calls[0]["project_id"] == "chirps-prod"
    assert firebase.calls[0]["email"].endswith("@loadtest.invalid")


def test_token_project_or_uid_mismatch_quarantines_without_bootstrap(tmp_path: Path) -> None:
    firebase = FakeFirebase()
    called = False
    p = plan(1)

    def bootstrap(**kwargs):
        nonlocal called
        called = True
        return {"id": str(uuid.uuid4()), "firebase_uid": "wrong"}

    with pytest.raises(provisioning.QuarantineRequired, match="project_or_identity"):
        provisioning.provision_users(
            plan=p, firebase=firebase,
            token_client=lambda **_: {"idToken": "secret", "localId": provisioning.users_for_plan(p)[0].uid},
            token_verifier=lambda _: {"uid": provisioning.users_for_plan(p)[0].uid, "aud": "wrong", "exp": int(NOW.timestamp()) + 3600},
            bootstrap=bootstrap, verify_ownership=lambda **kwargs: OWNERSHIP,
            manifest_path=tmp_path / "manifest.json", now=NOW,
        )
    assert not called


def test_firebase_create_unknown_is_quarantined_without_lookup_or_retry(tmp_path: Path) -> None:
    class Broken(FakeFirebase):
        def create_user(self, **kwargs):
            self.calls.append(kwargs)
            raise TimeoutError("provider details must not escape")

    firebase = Broken()
    with pytest.raises(provisioning.QuarantineRequired, match="firebase_create_unknown"):
        provisioning.provision_users(
            plan=plan(1), firebase=firebase,
            token_client=lambda **_: pytest.fail("token must not be requested"),
            token_verifier=lambda _: {}, bootstrap=lambda **_: {}, verify_ownership=lambda **kwargs: OWNERSHIP,
            manifest_path=tmp_path / "manifest.json", now=NOW,
        )
    assert len(firebase.calls) == 1


def test_bootstrap_unknown_is_quarantined(tmp_path: Path) -> None:
    p = plan(1)
    user = provisioning.users_for_plan(p)[0]
    with pytest.raises(provisioning.QuarantineRequired, match="bootstrap_unknown"):
        provisioning.provision_users(
            plan=p, firebase=FakeFirebase(),
            token_client=lambda **_: {"idToken": "t", "localId": user.uid},
            token_verifier=lambda _: {"uid": user.uid, "aud": "chirps-prod", "exp": int(NOW.timestamp()) + 3600},
            bootstrap=lambda **_: (_ for _ in ()).throw(TimeoutError()), verify_ownership=lambda **kwargs: OWNERSHIP,
            manifest_path=tmp_path / "manifest.json", now=NOW,
        )


def test_manifest_is_durable_and_ownership_is_required(tmp_path: Path) -> None:
    p = plan(1)
    user = provisioning.users_for_plan(p)[0]
    manifest = tmp_path / "manifest.json"
    with pytest.raises(provisioning.ProvisioningError, match="ownership_or_provisioning"):
        provisioning.provision_users(
            plan=p, firebase=FakeFirebase(),
            token_client=lambda **_: {"idToken": "t", "localId": user.uid},
            token_verifier=lambda _: {"uid": user.uid, "aud": "chirps-prod", "exp": int(NOW.timestamp()) + 3600},
            bootstrap=lambda **_: {"id": str(uuid.uuid4()), "firebase_uid": user.uid},
            verify_ownership=lambda **_: (_ for _ in ()).throw(RuntimeError("foreign row")),
            manifest_path=manifest, now=NOW,
        )
    saved = json.loads(manifest.read_text())
    assert saved["status"] == "QUARANTINED"
    assert saved["users"][0]["uid"] == user.uid
    assert "foreign row" not in manifest.read_text()


def test_empty_or_mismatched_ownership_cannot_be_marked_complete(tmp_path: Path) -> None:
    p = plan(1)
    user = provisioning.users_for_plan(p)[0]
    with pytest.raises(provisioning.ProvisioningError, match="ownership_mismatch"):
        provisioning.provision_users(
            plan=p, firebase=FakeFirebase(),
            token_client=lambda **_: {"idToken": "t", "localId": user.uid},
            token_verifier=lambda _: {"uid": user.uid, "aud": "chirps-prod", "exp": int(NOW.timestamp()) + 3600},
            bootstrap=lambda **_: {"id": str(uuid.uuid4()), "firebase_uid": user.uid},
            verify_ownership=lambda **_: (_ for _ in ()).throw(provisioning.ProvisioningError("ownership_mismatch")),
            manifest_path=tmp_path / "manifest.json",
            now=NOW,
        )


def test_bootstrap_url_refuses_redirect_prone_or_non_https_targets() -> None:
    with pytest.raises(provisioning.ProvisioningError, match="bootstrap_url"):
        provisioning.bootstrap_http_client(base_url="http://127.0.0.1:8000")
    with pytest.raises(provisioning.ProvisioningError, match="bootstrap_url"):
        provisioning.bootstrap_http_client(base_url="https://example.test/bootstrap?token=secret")
