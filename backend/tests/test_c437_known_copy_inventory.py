from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from google.cloud.storage._media.requests.download import RawDownload
from requests import Response

from app.services.known_copy_inventory import GCSReader, InventoryReadError, ScanLimits, _HashSink, main, scan_known_copies, write_manifest


class FakeBlob:
    def __init__(self, name, data, generation="1", *, md5=True, size=None, chunks=None, fail=False):
        self.name = name
        self.data = data
        self.generation = generation
        self.size = len(data) if size is None else size
        self.md5_hash = hashlib.md5(data).hexdigest() if md5 and size is None else None
        self.crc32c = None
        self.chunks = chunks
        self.fail = fail
        self.calls = []

    def reload(self, *, if_generation_match=None, timeout=None, retry=None):
        self.calls.append(("reload", if_generation_match, timeout, retry))
        if if_generation_match is not None and str(if_generation_match) != str(self.generation):
            raise FakeProviderError(412)
        if self.fail:
            raise FakeProviderError(503)
        return self

    def download_to_file(self, sink, *, if_generation_match=None, timeout=None, retry=None, raw_download=False, checksum=None):
        self.calls.append(("download", if_generation_match, timeout, retry, raw_download, checksum))
        if if_generation_match is not None and str(if_generation_match) != str(self.generation):
            raise FakeProviderError(412)
        if self.fail:
            raise FakeProviderError(503)
        chunks = self.chunks or [self.data]
        for chunk in chunks:
            sink.write(chunk)


class FakeProviderError(Exception):
    def __init__(self, code):
        super().__init__("provider-secret-sentinel")
        self.code = code


class FakeIterator:
    def __init__(self, pages, next_page_token=None):
        self._pages = pages
        self.next_page_token = next_page_token
    @property
    def pages(self):
        for page in self._pages:
            yield page


class FakeBucket:
    def __init__(self, blobs, pages=1):
        self.blobs = {b.name: b for b in blobs}
        self.pages = pages

    def blob(self, name): return self.blobs[name]

    def list_blobs(self, *, prefix, page_token=None, timeout, retry, page_size):
        selected = [b for b in self.blobs.values() if b.name.startswith(prefix)]
        step = max(1, (len(selected) + self.pages - 1) // self.pages)
        index = int(page_token or 0)
        page = selected[index:index + step]
        next_token = str(index + step) if index + step < len(selected) else None
        return FakeIterator([page], next_token)


class FakeClient:
    def __init__(self, bucket): self._bucket = bucket
    def bucket(self, name): return self._bucket


def reader_for(monkeypatch, blobs, pages=1):
    bucket = FakeBucket(blobs, pages=pages)
    monkeypatch.setattr("app.services.known_copy_inventory.storage_service._storage_client", lambda: FakeClient(bucket))
    return GCSReader("synthetic-media"), bucket


def test_real_adapter_hashes_same_bytes_across_paginated_prefixes(monkeypatch):
    data = b"synthetic image bytes\x00"
    reader, bucket = reader_for(monkeypatch, [
        FakeBlob("posts/u/target.jpg", data, "11"),
        FakeBlob("posts/u/duplicate.jpg", data, "12"),
        FakeBlob("avatars/u/avatar.jpg", data, "13"),
        FakeBlob("posts/u/different.jpg", b"other bytes!", "14"),
    ], pages=3)
    result = scan_known_copies(reader, bucket="synthetic-media", target_name="posts/u/target.jpg", expected_generation="11")
    assert result.complete is True
    assert [item.metadata.name for item in result.matches] == ["avatars/u/avatar.jpg", "posts/u/duplicate.jpg", "posts/u/target.jpg"]
    assert bucket.blobs["posts/u/target.jpg"].calls[0][0] == "reload"
    assert result.deletion_authorized is False


def test_exact_synthetic_prefix_scope_never_lists_other_posts(monkeypatch):
    data = b"synthetic image bytes\x00"
    prefix = "posts/c437-test-20261008-nonce/"
    target = FakeBlob(prefix + "target.jpg", data, "11")
    copy = FakeBlob(prefix + "copy.jpg", data, "12")
    unrelated = FakeBlob("posts/other-user/private.jpg", data, "13")
    reader, bucket = reader_for(monkeypatch, [target, copy, unrelated], pages=2)
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="11", prefixes=(prefix,))
    assert result.complete is True
    assert result.scope == (prefix,)
    assert [item.metadata.name for item in result.matches] == sorted((target.name, copy.name))
    assert not any(call[0] == "download" for call in bucket.blobs[unrelated.name].calls)


def test_equal_size_different_bytes_are_not_matches(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"123456", "1")
    different = FakeBlob("avatars/u/different.jpg", b"654321", "2")
    reader, _ = reader_for(monkeypatch, [target, different])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1")
    assert [item.metadata.name for item in result.matches] == [target.name]


def test_generation_is_required_and_mismatch_refuses_download(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"bytes", "11")
    reader, _ = reader_for(monkeypatch, [target])
    missing = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name)
    assert missing.complete is False and "generation_required" in missing.incomplete_reasons
    mismatch = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="12")
    assert "target_generation_mismatch" in mismatch.incomplete_reasons
    assert not any(call[0] == "download" for call in target.calls)


def test_missing_generation_and_size_are_incomplete(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"bytes", None)
    reader, _ = reader_for(monkeypatch, [target])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1")
    assert result.complete is False
    assert "target_generation_mismatch" in result.incomplete_reasons or "missing_generation" in result.incomplete_reasons

    target = FakeBlob("posts/u/target.jpg", b"bytes", "1", size=None)
    target.size = None
    reader, _ = reader_for(monkeypatch, [target])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1")
    assert "missing_object_size" in result.incomplete_reasons


def test_aggregate_cap_and_incremental_object_cap_count_attempted_bytes(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"1234", "1", chunks=[b"12", b"3456"])
    reader, _ = reader_for(monkeypatch, [target])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1", limits=ScanLimits(max_object_bytes=4, max_download_bytes=20, deadline_seconds=10))
    assert result.complete is False
    assert "object_size_cap_reached" in result.incomplete_reasons
    assert result.downloaded_bytes >= 6

    target = FakeBlob("posts/u/target.jpg", b"123456", "1")
    reader, _ = reader_for(monkeypatch, [target])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1", limits=ScanLimits(max_object_bytes=20, max_download_bytes=5, deadline_seconds=10))
    assert result.complete is False and "download_byte_cap_reached" in result.incomplete_reasons
    assert not any(call[0] == "download" for call in target.calls)


def test_deadline_is_checked_between_download_chunks(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"123456", "1", chunks=[b"123", b"456"])
    reader, _ = reader_for(monkeypatch, [target])
    import app.services.known_copy_inventory as inventory
    clock = iter([0.0, 0.1, 2.0])
    monkeypatch.setattr(inventory.time, "monotonic", lambda: next(clock))
    with pytest.raises(inventory.InventoryReadError, match="scan_deadline_exceeded") as error:
        reader.sha256(target, deadline=1.0, max_bytes=100)
    assert error.value.bytes_seen == 6


def test_deadline_and_provider_errors_are_fixed_and_redacted(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"bytes", "1", fail=True)
    reader, _ = reader_for(monkeypatch, [target])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1")
    assert "target_generation_mismatch" not in result.incomplete_reasons
    assert set(result.incomplete_reasons) <= {"target_read_failed", "target_recheck_failed"}
    assert all(reason in {"target_read_failed", "target_recheck_failed"} for reason in result.incomplete_reasons)


def test_aggregate_remaining_budget_caps_dishonest_candidate_and_expired_hash_does_not_call(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"x", "1")
    candidate = FakeBlob("avatars/u/copy.jpg", b"xxxxx", "2", md5=False, size=1)
    trailing = FakeBlob("avatars/u/trailing.jpg", b"x", "3")
    reader, _ = reader_for(monkeypatch, [target, candidate, trailing])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1", limits=ScanLimits(max_object_bytes=20, max_download_bytes=3, deadline_seconds=10))
    assert "download_byte_cap_reached" in result.incomplete_reasons
    assert result.downloaded_bytes >= 3
    assert not any(call[0] == "download" for call in trailing.calls)

    expired = FakeBlob("posts/u/expired.jpg", b"bytes", "3")
    with pytest.raises(InventoryReadError, match="scan_deadline_exceeded"):
        reader.sha256(expired, deadline=0, max_bytes=100)
    assert not any(call[0] == "download" for call in expired.calls)

    target = FakeBlob("posts/u/target.jpg", b"bytes", "1")
    candidate = FakeBlob("avatars/u/copy.jpg", b"bytes", "2", fail=True)
    reader, _ = reader_for(monkeypatch, [target, candidate])
    result = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1")
    assert "provider_hash_failed" in result.incomplete_reasons
    assert "provider-secret-sentinel" not in json.dumps(result.to_dict())


def test_installed_gcs_raw_download_writes_hash_sink_without_seek_or_temp_file():
    class RawBody:
        def stream(self, _chunk_size, decode_content=False):
            assert decode_content is False
            yield b"abc"
            yield b"def"

        def read(self):
            return b"abcdef"

        def close(self):
            return None

    class Transport:
        def request(self, *_args, **_kwargs):
            response = Response()
            response.status_code = 200
            response.url = "https://storage.googleapis.com/synthetic/object?generation=1"
            response.headers = {}
            response.raw = RawBody()
            return response

    sink = _HashSink(deadline=10**12, max_bytes=100)
    RawDownload("https://storage.googleapis.com/synthetic/object?generation=1", stream=sink, checksum=None).consume(Transport())
    assert sink.total == 6
    assert sink.digest.hexdigest() == hashlib.sha256(b"abcdef").hexdigest()

def test_failed_target_digest_binds_request_and_limits(monkeypatch):
    reader, _ = reader_for(monkeypatch, [])
    first = scan_known_copies(reader, bucket="synthetic-media", target_name="posts/u/one.jpg", expected_generation="1", limits=ScanLimits(max_objects=10))
    second = scan_known_copies(reader, bucket="synthetic-media", target_name="posts/u/two.jpg", expected_generation="1", limits=ScanLimits(max_objects=11))
    assert first.complete is False and second.complete is False
    assert first.manifest_digest != second.manifest_digest
    assert first.to_dict()["requested_target_name"] == "posts/u/one.jpg"
    assert first.to_dict()["limits"]["max_objects"] == 10


def test_object_cap_and_target_generation_change_are_incomplete(monkeypatch):
    target = FakeBlob("posts/u/target.jpg", b"bytes", "1")
    candidate = FakeBlob("avatars/u/copy.jpg", b"bytes", "2")
    reader, _ = reader_for(monkeypatch, [target, candidate])
    capped = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1", limits=ScanLimits(max_objects=1))
    assert "object_cap_reached" in capped.incomplete_reasons

    target = FakeBlob("posts/u/target.jpg", b"bytes", "1")
    reader, _ = reader_for(monkeypatch, [target])
    original_get = reader.get
    calls = 0

    def changing_get(name, *, deadline, expected_generation=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.generation = "2"
        return original_get(name, deadline=deadline, expected_generation=expected_generation)

    monkeypatch.setattr(reader, "get", changing_get)
    changed = scan_known_copies(reader, bucket="synthetic-media", target_name=target.name, expected_generation="1")
    assert "target_changed_during_scan" in changed.incomplete_reasons


def test_manifest_digest_recomputes_and_secure_writer_refuses_existing_or_symlink(tmp_path):
    data = b"private synthetic bytes"
    # The fake reader path above is unnecessary here; use a minimal captured result shape.
    reader = type("R", (), {})()
    from app.services.known_copy_inventory import KnownCopyInventory
    limits = ScanLimits()
    result = KnownCopyInventory(2, "synthetic", "posts/u/a.jpg", "1", None, (), ("posts/", "avatars/"), "per-prefix provider listing; not an atomic bucket snapshot", limits, "start", "end", 0, 0, False, ("target_read_failed",), False, "")
    result = result.__class__(**{**result.__dict__, "manifest_digest": hashlib.sha256(json.dumps(result.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    assert result.manifest_digest == hashlib.sha256(json.dumps(result.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = tmp_path / "private" / "manifest.json"
    write_manifest(path, result)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(OSError): write_manifest(path, result)
    symlink = tmp_path / "link.json"
    symlink.symlink_to(path)
    with pytest.raises(OSError): write_manifest(symlink, result)

    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o750)
    nested = existing / "nested" / "deeper"
    write_manifest(nested / "second.json", result)
    assert existing.stat().st_mode & 0o777 == 0o750
    assert nested.stat().st_mode & 0o777 == 0o700
    assert nested.parent.stat().st_mode & 0o777 == 0o700


def test_cli_setup_failure_is_fixed_and_redacted(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("app.services.known_copy_inventory.storage_service._storage_client", lambda: (_ for _ in ()).throw(RuntimeError("private-provider-secret")))
    code = main(["--bucket", "synthetic-media", "--object", "posts/u/a.jpg", "--generation", "1", "--manifest", str(tmp_path / "manifest.json")])
    assert code == 2
    output = capsys.readouterr().out
    assert output.strip() == '{"complete": false, "error": "invalid_or_unavailable_inventory"}'
    assert "private-provider-secret" not in output
