from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
try:
    from c437_synthetic_fixture import _cleanup_created_generations, main
finally:
    sys.path.pop(0)


def test_fixture_defaults_to_print_only_and_strict_prefix(capsys):
    assert main(["--project", "chirps-prod", "--bucket", "chirps-prod-media", "--nonce", "abc12345"]) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["mode"] == "PRINT_ONLY_NOT_EXECUTED"
    assert packet["prefix"].startswith("posts/c437-test-")
    assert len(packet["synthetic_objects"]) == 3
    assert all(name.startswith(packet["prefix"]) for name in packet["synthetic_objects"])


@pytest.mark.parametrize("args", [
    ["--project", "chirps-prod", "--bucket", "chirps-prod-media", "--nonce", "bad/nonce"],
    ["--project", "chirps-prod", "--bucket", "chirps-prod-media", "--nonce", "abc12345", "--execute"],
])
def test_fixture_rejects_unsafe_or_unapproved_execution(args, capsys):
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["mode"] == "ERROR"


def test_fixture_rejects_non_authorized_bucket(capsys):
    assert main(["--project", "chirps-prod", "--bucket", "chirp-media", "--nonce", "abc12345"]) == 2
    assert json.loads(capsys.readouterr().out)["mode"] == "ERROR"


def test_execute_runs_real_inventory_plan_delete_verify_and_replacement_with_fake_sdk(monkeypatch, tmp_path, capsys):
    class ProviderError(Exception):
        def __init__(self, code):
            self.code = code

    class Blob:
        def __init__(self, bucket, name):
            self.bucket = bucket
            self.name = name
            self.data = None
            self.generation = None
            self.size = None
            self.md5_hash = None
            self.crc32c = None

        def upload_from_string(self, data, *, content_type, if_generation_match, retry, timeout=None):
            if if_generation_match == 0 and self.data is not None:
                raise ProviderError(412)
            self.data = data
            self.generation = str(int(self.generation or 0) + 1)
            self.size = len(data)
            self.md5_hash = hashlib.md5(data).hexdigest()

        def reload(self, *, if_generation_match=None, timeout=None, retry=None):
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

    class Iterator:
        def __init__(self, rows):
            self.next_page_token = None
            self.rows = rows

        @property
        def pages(self):
            yield self.rows

    class Bucket:
        def __init__(self):
            self.items = {}
            self.deleted = []

        def blob(self, name):
            return self.items.setdefault(name, Blob(self, name))

        def list_blobs(self, *, prefix, page_token, timeout, retry, page_size):
            return Iterator([item for name, item in self.items.items() if name.startswith(prefix) and item.data is not None])

    class Client:
        def __init__(self, project):
            self.media = Bucket()

        def bucket(self, name):
            assert name == "chirps-prod-media"
            return self.media

    fake_client = Client("chirps-prod")
    monkeypatch.setattr("google.cloud.storage.Client", lambda project: fake_client)
    receipt = tmp_path / "receipt.json"
    assert main(["--project", "chirps-prod", "--bucket", "chirps-prod-media", "--nonce", "abc12345", "--execute", "--approval", "I_UNDERSTAND_SYNTHETIC_ONLY", "--receipt", str(receipt)]) == 0
    packet = json.loads(capsys.readouterr().out)
    result = packet["result"]
    assert result["verification_complete"] is False
    replacement_name = next(name for name in fake_client.media.items if name.endswith("copy.bin"))
    assert (replacement_name, "1") in fake_client.media.deleted
    assert (replacement_name, "2") not in fake_client.media.deleted
    raw = json.loads(receipt.read_text(encoding="utf-8"))
    assert any(item["status"] == "reappeared" for item in raw["outcomes"])


def test_failure_cleanup_is_exact_generation_bound_and_journaled(tmp_path):
    class Provider:
        def __init__(self):
            self.calls = []

        def delete(self, name, generation, *, deadline):
            self.calls.append((name, generation))
            if name.endswith("copy.bin"):
                raise RuntimeError("provider detail must not enter evidence")

    provider = Provider()
    created = [("posts/c437-test-nonce/target.bin", "11"), ("posts/c437-test-nonce/copy.bin", "12")]
    evidence = tmp_path / "private" / "receipt.cleanup.json"
    outcomes = _cleanup_created_generations(provider, "chirps-prod-media", "posts/c437-test-nonce/", evidence, created)
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
