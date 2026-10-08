from __future__ import annotations

import hashlib
import json

import pytest

from app.services.known_copy_inventory import KnownCopyInventory, ManifestObject, ObjectMetadata, ScanLimits
from app.services.known_copy_removal import (
    RemovalError,
    GCSRemovalProvider,
    RemovalObject,
    build_removal_plan,
    execute_removal,
    receipt_reference,
    verify_removal,
)


def _inventory(*, complete=True, target_generation="11"):
    target = ManifestObject(ObjectMetadata("posts/u/target.jpg", target_generation, 4, "md5", None), hashlib.sha256(b"data").hexdigest())
    copy = ManifestObject(ObjectMetadata("posts/u/copy.jpg", "12", 4, "md5", None), target.sha256)
    avatar = ManifestObject(ObjectMetadata("avatars/u/avatar.jpg", "13", 4, "md5", None), target.sha256)
    result = KnownCopyInventory(2, "synthetic-media", target.metadata.name, target_generation, target, (target, copy, avatar), ("posts/", "avatars/"), "synthetic non-atomic listing", ScanLimits(), "start", "end", 3, 12, complete, () if complete else ("scan_deadline_exceeded",), False, "")
    digest = hashlib.sha256(json.dumps(result.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result.__class__(**{**result.__dict__, "manifest_digest": digest})


class FakeProvider:
    def __init__(self, generations):
        self.generations = dict(generations)
        self.deleted = []
        self.fail_delete = set()

    def read(self, name, *, deadline):
        generation = self.generations.get(name)
        if generation is None:
            return None
        return ObjectMetadata(name, generation, 4, "md5", None)

    def delete(self, name, generation, *, deadline):
        self.deleted.append((name, generation))
        if name in self.fail_delete:
            raise RemovalError("provider_delete_failed")
        if self.generations.get(name) == generation:
            del self.generations[name]


def test_plan_requires_complete_inventory_and_scopes_to_reviewed_posts():
    with pytest.raises(RemovalError, match="inventory_incomplete"):
        build_removal_plan(_inventory(complete=False))
    plan = build_removal_plan(_inventory())
    assert plan.allowed_prefixes == ("posts/",)
    assert [item.name for item in plan.objects] == ["posts/u/copy.jpg", "posts/u/target.jpg"]
    assert plan.plan_digest == hashlib.sha256(json.dumps(plan.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_execute_is_generation_bound_durable_and_idempotent(tmp_path):
    plan = build_removal_plan(_inventory())
    provider = FakeProvider({"posts/u/target.jpg": "11", "posts/u/copy.jpg": "12"})
    receipt_path = tmp_path / "private" / "removal.json"
    receipt = execute_removal(plan, provider, receipt_path)
    assert all(item.status == "removed_verified" for item in receipt.outcomes)
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert receipt_reference(receipt).startswith("c437-removal:")
    deleted = list(provider.deleted)
    again = execute_removal(plan, provider, receipt_path)
    assert again.receipt_digest == receipt.receipt_digest
    assert provider.deleted == deleted


def test_stale_generation_and_reappearance_are_never_deleted(tmp_path):
    plan = build_removal_plan(_inventory())
    provider = FakeProvider({"posts/u/target.jpg": "99", "posts/u/copy.jpg": "12"})
    receipt = execute_removal(plan, provider, tmp_path / "receipt.json")
    target = next(item for item in receipt.outcomes if item.object.name.endswith("target.jpg"))
    assert target.status == "reappeared"
    assert ("posts/u/target.jpg", "11") not in provider.deleted

    provider.generations["posts/u/copy.jpg"] = "77"
    checked = verify_removal(receipt, provider)
    copy = next(item for item in checked.outcomes if item.object.name.endswith("copy.jpg"))
    assert copy.status == "reappeared"
    assert copy.observed_generation == "77"
    with pytest.raises(RemovalError, match="receipt_invalid"):
        receipt_reference(checked)


def test_failed_delete_can_retry_same_plan_without_scope_expansion(tmp_path):
    plan = build_removal_plan(_inventory())
    provider = FakeProvider({"posts/u/target.jpg": "11", "posts/u/copy.jpg": "12"})
    provider.fail_delete.add("posts/u/copy.jpg")
    receipt_path = tmp_path / "receipt.json"
    first = execute_removal(plan, provider, receipt_path)
    assert any(item.status == "failed" for item in first.outcomes)
    provider.fail_delete.clear()
    second = execute_removal(plan, provider, receipt_path)
    assert all(item.status == "removed_verified" for item in second.outcomes)
    assert {name for name, _ in provider.deleted} == {"posts/u/target.jpg", "posts/u/copy.jpg"}
    assert all(generation in {"11", "12"} for _, generation in provider.deleted)


def test_receipt_mismatch_rejects_changed_scope(tmp_path):
    plan = build_removal_plan(_inventory())
    provider = FakeProvider({"posts/u/target.jpg": "11", "posts/u/copy.jpg": "12"})
    receipt_path = tmp_path / "receipt.json"
    execute_removal(plan, provider, receipt_path)
    other = build_removal_plan(_inventory(target_generation="99"))
    with pytest.raises(RemovalError, match="receipt_mismatch"):
        execute_removal(other, provider, receipt_path)


def test_gcs_adapter_uses_generation_precondition_and_no_retry(monkeypatch):
    class Blob:
        name = "posts/synthetic/object"
        generation = "7"
        size = 4
        md5_hash = "md5"
        crc32c = None

        def __init__(self):
            self.calls = []

        def delete(self, **kwargs):
            self.calls.append(("delete", kwargs))

        def reload(self, **kwargs):
            self.calls.append(("reload", kwargs))

    class Bucket:
        def __init__(self):
            self.item = Blob()

        def blob(self, _name):
            return self.item

    bucket = Bucket()
    monkeypatch.setattr("app.services.known_copy_removal.storage_service._storage_client", lambda: type("Client", (), {"bucket": lambda self, _name: bucket})())
    provider = GCSRemovalProvider("synthetic-media")
    provider.delete("posts/synthetic/object", "7", deadline=10**12)
    metadata = provider.read("posts/synthetic/object", deadline=10**12)
    assert metadata is not None and metadata.generation == "7"
    assert bucket.item.calls[0][1]["if_generation_match"] == 7
    assert bucket.item.calls[0][1]["retry"] is None
    assert bucket.item.calls[1][1]["retry"] is None
