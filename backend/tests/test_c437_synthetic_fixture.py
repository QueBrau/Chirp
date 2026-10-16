from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
try:
    import c437_synthetic_fixture as fixture
finally:
    sys.path.pop(0)


PROJECT = ["--project", "chirps-prod", "--project-number", "593616178468", "--nonce", "abc12345", "--cleanup"]


class ProviderError(Exception):
    def __init__(self, code: int):
        self.code = code


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.data = None
        self.generation = None
        self.size = None
        self.md5_hash = None
        self.crc32c = None
        self.metadata = None

    def upload_from_string(self, data, *, content_type, if_generation_match, retry, timeout=None):
        assert content_type == "application/octet-stream"
        if if_generation_match == 0 and self.data is not None:
            raise ProviderError(412)
        self.data = data
        self.generation = str(self.bucket.generations.get(self.name, 0) + 1)
        self.bucket.generations[self.name] = int(self.generation)
        self.size = len(data)
        self.md5_hash = hashlib.md5(data).hexdigest()
        self.bucket.objects[self.name] = self

    def reload(self, *, if_generation_match=None, timeout=None, retry=None):
        if self.name in self.bucket.reload_fail_names:
            raise RuntimeError("readback unavailable")
        if self.data is None:
            raise ProviderError(404)
        if if_generation_match is not None and str(if_generation_match) != self.generation:
            raise ProviderError(412)
        return self

    def download_to_file(self, sink, *, if_generation_match, timeout, retry, raw_download, checksum):
        self.reload(if_generation_match=if_generation_match, timeout=timeout, retry=retry)
        sink.write(self.data)

    def delete(self, *, if_generation_match, timeout, retry):
        self.reload(if_generation_match=if_generation_match, timeout=timeout, retry=retry)
        self.bucket.deleted.append((self.name, self.generation))
        self.data = None
        self.size = None
        self.bucket.objects.pop(self.name, None)


class FakeIterator:
    def __init__(self, rows):
        self.next_page_token = None
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    @property
    def pages(self):
        yield self.rows


class FakeBucket:
    def __init__(self, *, project_number="593616178468", purpose="c437-synthetic"):
        self.name = "chirps-c437-fixture-593616178468-abc12345"
        self.project_number = project_number
        self.labels = {"purpose": purpose}
        self.iam_configuration = type("IAM", (), {
            "uniform_bucket_level_access_enabled": True,
            "public_access_prevention": "enforced",
        })()
        self.versioning_enabled = False
        self.soft_delete_policy = type("SoftDelete", (), {"retention_duration_seconds": 0})()
        self.retention_period = None
        self.retention_policy_locked = False
        self.object_retention_mode = None
        self.default_event_based_hold = False
        self.objects = {}
        self.generations = {}
        self.reload_fail_names = set()
        self.deleted = []
        self.list_calls = []

    def reload(self, *, timeout, retry):
        return self

    def blob(self, name):
        return self.objects.get(name) or FakeBlob(self, name)

    def list_blobs(self, prefix=None, page_token=None, timeout=None, retry=None, page_size=None, max_results=None):
        self.list_calls.append({"prefix": prefix, "page_token": page_token, "timeout": timeout,
                                "retry": retry, "page_size": page_size, "max_results": max_results})
        rows = [item for name, item in self.objects.items() if prefix is None or name.startswith(prefix)]
        if max_results is not None:
            rows = rows[:max_results]
        return FakeIterator(rows)


class FakeClient:
    def __init__(self, bucket=None):
        self.media = bucket or FakeBucket()
        self.project = "chirps-prod"

    def bucket(self, name):
        assert name == self.media.name
        return self.media


def _execute_args(tmp_path, digest):
    return PROJECT + [
        "--execute", "--approval", fixture.APPROVAL, "--confirm-plan-digest", digest,
        "--receipt", str(tmp_path / "receipt.json"), "--cleanup",
    ]


def _plan_digest(capsys):
    assert fixture.main(PROJECT) == 0
    return json.loads(capsys.readouterr().out)["plan_digest"]


def test_fixture_defaults_to_print_only_and_derived_disposable_scope(monkeypatch, capsys):
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: pytest.fail("print-only imported provider"))
    assert fixture.main(PROJECT) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["mode"] == "PRINT_ONLY_NOT_EXECUTED"
    assert packet["project"] == "chirps-prod"
    assert packet["project_number"] == "593616178468"
    assert packet["bucket"] == "chirps-c437-fixture-593616178468-abc12345"
    assert packet["prefix"] == "posts/c437-test-abc12345/"
    assert len(packet["synthetic_objects"]) == 3
    assert all(name.startswith(packet["prefix"]) for name in packet["synthetic_objects"])
    assert len(packet["plan_digest"]) == 64


@pytest.mark.parametrize("args", [
    ["--project", "chirps-prod", "--project-number", "593616178468", "--nonce", "bad/nonce"],
    ["--project", "other-project", "--project-number", "593616178468", "--nonce", "abc12345"],
    ["--project", "chirps-prod", "--project-number", "999", "--nonce", "abc12345"],
])
def test_fixture_refuses_wrong_scope_before_provider_import(monkeypatch, args, capsys):
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: pytest.fail("provider imported before scope validation"))
    assert fixture.main(args) == 2
    packet = json.loads(capsys.readouterr().out)
    assert packet["mode"] == "REFUSED"


def test_execute_refusals_happen_before_sdk_import(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: pytest.fail("SDK imported before local refusal"))
    digest = "0" * 64
    cases = [
        PROJECT + ["--execute"],
        PROJECT + ["--execute", "--approval", fixture.APPROVAL, "--confirm-plan-digest", digest,
                   "--receipt", str(tmp_path / "relative.json"), "--cleanup"],
        PROJECT + ["--execute", "--approval", fixture.APPROVAL, "--confirm-plan-digest", "f" * 64,
                   "--receipt", str(tmp_path / "receipt.json"), "--cleanup"],
    ]
    for args in cases:
        assert fixture.main(args) == 2
        assert json.loads(capsys.readouterr().out)["mode"] == "REFUSED"


def test_execute_requires_gcs_specific_telemetry_guard_before_sdk_import(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", raising=False)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: pytest.fail("SDK imported before telemetry guard"))
    digest = _plan_digest(capsys)
    assert fixture.main(_execute_args(tmp_path, digest)) == 2
    packet = json.loads(capsys.readouterr().out)
    assert packet["reason"] == "gcs_bucket_metadata_telemetry_must_be_disabled"


@pytest.mark.parametrize("patch", [
    {"project_number": "111"},
    {"purpose": "production-media"},
])
def test_execute_rejects_bucket_metadata_before_upload(monkeypatch, tmp_path, capsys, patch):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    monkeypatch.setattr(fixture, "EXPECTED_PROJECT", "chirps-prod")
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: FakeClient(FakeBucket(**patch)))
    digest = _plan_digest(capsys)
    args = _execute_args(tmp_path, digest)
    assert fixture.main(args) == 2
    packet = json.loads(capsys.readouterr().out)
    assert packet["phase"] == "preflight"
    assert packet["reason"] == "bucket_safety_metadata_mismatch"


@pytest.mark.parametrize("field,value", [
    ("versioning_enabled", True),
    ("soft_delete_policy", type("SoftDelete", (), {"retention_duration_seconds": 604800})()),
    ("retention_period", 86400),
    ("object_retention_mode", "Enabled"),
])
def test_execute_rejects_retention_or_versioning_before_upload(monkeypatch, tmp_path, capsys, field, value):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    setattr(bucket, field, value)
    client = FakeClient(bucket)
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: client)
    digest = _plan_digest(capsys)
    assert fixture.main(_execute_args(tmp_path, digest)) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "bucket_safety_metadata_mismatch"
    assert bucket.objects == {}


def test_execute_refuses_nonempty_bucket(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    stray = FakeBlob(bucket, "unrelated/object")
    stray.data, stray.generation, stray.size = b"unrelated", "1", 9
    bucket.objects[stray.name] = stray
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: FakeClient(bucket))
    digest = _plan_digest(capsys)
    assert fixture.main(_execute_args(tmp_path, digest)) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "bucket_not_empty"
    assert bucket.deleted == []


def test_empty_bucket_preflight_listing_is_bounded_and_does_not_retry(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: FakeClient(bucket))
    digest = _plan_digest(capsys)
    assert fixture.main(_execute_args(tmp_path, digest)) == 0
    preflight = bucket.list_calls[0]
    assert preflight == {"prefix": None, "page_token": None, "timeout": 5,
                         "retry": None, "page_size": None, "max_results": 1}


def test_inventory_refuses_foreign_matching_generation_and_cleans_only_own_generations(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: FakeClient(bucket))
    from app.services import known_copy_inventory
    original_scan = known_copy_inventory.scan_known_copies

    def add_foreign_match_then_scan(*args, **kwargs):
        foreign = FakeBlob(bucket, "posts/c437-test-abc12345/foreign.bin")
        foreign.data = fixture.FIXTURE_BODY
        foreign.generation = "41"
        foreign.size = len(foreign.data)
        foreign.md5_hash = hashlib.md5(foreign.data).hexdigest()
        foreign.metadata = {"purpose": "c437-synthetic", "run_nonce": "abc12345"}
        bucket.objects[foreign.name] = foreign
        bucket.generations[foreign.name] = 41
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(known_copy_inventory, "scan_known_copies", add_foreign_match_then_scan)
    digest = _plan_digest(capsys)
    assert fixture.main(_execute_args(tmp_path, digest)) == 2
    packet = json.loads(capsys.readouterr().out)
    assert packet["reason"] == "fixture_inventory_object_set_mismatch"
    assert set(bucket.deleted) == {
        ("posts/c437-test-abc12345/target.bin", "1"),
        ("posts/c437-test-abc12345/copy.bin", "1"),
    }
    assert bucket.objects["posts/c437-test-abc12345/foreign.bin"].generation == "41"
    cleanup = json.loads((tmp_path / "receipt.json.cleanup.json").read_text(encoding="utf-8"))
    assert cleanup["complete"] is True
    assert cleanup["created_generations"] == [
        {"name": "posts/c437-test-abc12345/target.bin", "generation": "1"},
        {"name": "posts/c437-test-abc12345/copy.bin", "generation": "1"},
    ]


def test_keyboard_interrupt_during_upload_journals_unknown_name_without_deleting(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    client = FakeClient(bucket)
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: client)
    original_upload = FakeBlob.upload_from_string

    def write_then_interrupt(self, data, **kwargs):
        original_upload(self, data, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeBlob, "upload_from_string", write_then_interrupt)
    digest = _plan_digest(capsys)
    with pytest.raises(KeyboardInterrupt):
        fixture.main(_execute_args(tmp_path, digest))
    assert bucket.deleted == []
    cleanup = json.loads((tmp_path / "receipt.json.cleanup.json").read_text(encoding="utf-8"))
    assert cleanup["complete"] is False
    assert cleanup["unresolved_names"] == ["posts/c437-test-abc12345/target.bin"]
    assert bucket.objects["posts/c437-test-abc12345/target.bin"].generation == "1"


def test_keyboard_interrupt_on_second_upload_cleans_only_first_confirmed_generation(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    client = FakeClient(bucket)
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: client)
    original_upload = FakeBlob.upload_from_string

    def interrupt_second_upload(self, data, **kwargs):
        original_upload(self, data, **kwargs)
        if self.name.endswith("copy.bin"):
            raise KeyboardInterrupt

    monkeypatch.setattr(FakeBlob, "upload_from_string", interrupt_second_upload)
    digest = _plan_digest(capsys)
    with pytest.raises(KeyboardInterrupt):
        fixture.main(_execute_args(tmp_path, digest))
    assert bucket.deleted == [("posts/c437-test-abc12345/target.bin", "1")]
    assert "posts/c437-test-abc12345/target.bin" not in bucket.objects
    assert bucket.objects["posts/c437-test-abc12345/copy.bin"].generation == "1"
    cleanup = json.loads((tmp_path / "receipt.json.cleanup.json").read_text(encoding="utf-8"))
    assert cleanup["complete"] is False
    assert cleanup["created_generations"] == [
        {"name": "posts/c437-test-abc12345/target.bin", "generation": "1"},
    ]
    assert cleanup["unresolved_names"] == ["posts/c437-test-abc12345/copy.bin"]


def test_execute_runs_inventory_removal_replacement_and_exact_cleanup(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    client = FakeClient(bucket)
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: client)
    from app.services import storage_service
    sentinel = object()
    storage_service._client = sentinel
    digest = _plan_digest(capsys)

    assert fixture.main(_execute_args(tmp_path, digest)) == 0
    packet = json.loads(capsys.readouterr().out)
    result = packet["result"]
    assert result["cleanup_complete"] is True
    assert result["verification_complete"] is False  # reappearance is intentionally recorded
    replacement_name = next(name for name, generation in bucket.deleted if name.endswith("copy.bin") and generation == "1")
    assert (replacement_name, "2") in bucket.deleted
    assert storage_service._client is sentinel
    raw = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert any(item["status"] == "reappeared" for item in raw["outcomes"])
    for suffix in ("", ".inventory.json", ".plan.json", ".cleanup.json"):
        path = tmp_path / f"receipt.json{suffix}"
        assert path.stat().st_mode & 0o777 == 0o600
    cleanup = json.loads((tmp_path / "receipt.json.cleanup.json").read_text(encoding="utf-8"))
    assert cleanup["complete"] is True
    assert cleanup["unresolved_names"] == []


def test_ambiguous_upload_response_is_not_assumed_absent(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    bucket = FakeBucket()
    client = FakeClient(bucket)
    monkeypatch.setattr(fixture, "_new_storage_client", lambda *_: client)
    digest = _plan_digest(capsys)
    original_upload = FakeBlob.upload_from_string

    def write_then_lose_response(self, data, **kwargs):
        original_upload(self, data, **kwargs)
        raise RuntimeError("lost response")

    monkeypatch.setattr(FakeBlob, "upload_from_string", write_then_lose_response)
    assert fixture.main(_execute_args(tmp_path, digest)) == 2
    packet = json.loads(capsys.readouterr().out)
    assert packet["reason"] == "upload_outcome_unknown"
    assert not bucket.deleted  # no generation was confirmed, so deletion is unsafe
    assert bucket.objects["posts/c437-test-abc12345/target.bin"].generation == "1"
    cleanup = json.loads((tmp_path / "receipt.json.cleanup.json").read_text(encoding="utf-8"))
    assert cleanup["complete"] is False
    assert cleanup["unresolved_names"] == ["posts/c437-test-abc12345/target.bin"]
def test_failure_cleanup_is_exact_generation_bound_and_journaled(tmp_path):
    class Provider:
        def __init__(self):
            self.calls = []

        def delete(self, name, generation, *, deadline):
            self.calls.append((name, generation))
            if name.endswith("copy.bin"):
                raise RuntimeError("provider detail must not enter evidence")

        def read(self, name, *, deadline):
            return None

    provider = Provider()
    created = [("posts/c437-test-abc12345/target.bin", "11"), ("posts/c437-test-abc12345/copy.bin", "12")]
    evidence = tmp_path / "private" / "receipt.cleanup.json"
    evidence.parent.mkdir(mode=0o700)
    outcomes = fixture._cleanup_created_generations(provider, "chirps-c437-fixture-593616178468-abc12345", "posts/c437-test-abc12345/", evidence, created)
    assert provider.calls == created
    assert outcomes == [
        {"name": created[0][0], "generation": "11", "status": "deleted"},
        {"name": created[1][0], "generation": "12", "status": "failed", "reason": "cleanup_failed"},
    ]
    raw = json.loads(evidence.read_text(encoding="utf-8"))
    assert raw["complete"] is False
    assert raw["created_generations"] == [{"name": n, "generation": g} for n, g in created]
    assert "provider detail" not in evidence.read_text(encoding="utf-8")
    assert evidence.stat().st_mode & 0o777 == 0o600
