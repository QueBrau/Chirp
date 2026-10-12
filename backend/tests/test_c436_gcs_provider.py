from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import models
from app.db import get_session_factory
from app.services.account_provider_gcs import (
    FreezeReceipt,
    GCSMediaProvider,
    GCSMediaReader,
    MediaProviderUnavailable,
    MediaObject,
    MediaSnapshot,
    snapshot_user_media,
)
from app.services.account_provider_gcs import _digest, PREFIXES


class FakeReader:
    def __init__(self, objects: list[tuple[str, str]]) -> None:
        self.objects = {name: generation for name, generation in objects}
        self.calls: list[tuple[str, str]] = []
        self.keep_after_delete = False

    def list(self, prefix: str, *, deadline: float):
        self.calls.append(("list", prefix))
        return [
            SimpleNamespace(name=name, generation=generation)
            for name, generation in sorted(self.objects.items())
            if name.startswith(prefix)
        ]

    def read(self, name: str, *, deadline: float):
        self.calls.append(("read", name))
        generation = self.objects.get(name)
        return SimpleNamespace(name=name, generation=generation) if generation else None

    def delete(self, name: str, generation: str, *, deadline: float):
        self.calls.append(("delete", name))
        if not self.keep_after_delete:
            self.objects.pop(name, None)


def freeze(user_id: str, bucket: str = "chirp-media") -> FreezeReceipt:
    return FreezeReceipt(user_id, bucket, "freeze-c436", time.time() + 300)


def user_model(user_id: str):
    return SimpleNamespace(id=uuid.UUID(user_id))


def empty_snapshot(user_id: str, *, objects=(), digest=None):
    payload = {
        "user_id": user_id, "bucket": "chirp-media", "prefixes": list(PREFIXES),
        "objects": [item.payload() for item in objects], "freeze_id": "freeze-c436",
    }
    return MediaSnapshot(user_id, "chirp-media", PREFIXES, tuple(objects), "freeze-c436", digest or _digest(payload))


@pytest.mark.asyncio
async def test_snapshot_owns_tmp_posts_avatars_and_provider_deletes_all(
    make_user,
):
    user = await make_user("GCS media owner")
    reader = FakeReader([
        (f"tmp/{user.id}/upload.jpg", "11"),
        (f"posts/{user.id}/post.jpg", "22"),
        (f"avatars/{user.id}/avatar.jpg", "33"),
    ])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(user.id), reader, freeze(user.id), bucket="chirp-media"
        )
    assert {item.name for item in snapshot.objects} == {
        f"tmp/{user.id}/upload.jpg",
        f"posts/{user.id}/post.jpg",
        f"avatars/{user.id}/avatar.jpg",
    }
    result = await GCSMediaProvider(reader, bucket="chirp-media", snapshot=snapshot, freeze=freeze(user.id)).delete_account_data(
        user=user_model(user.id), idempotency_key="step-media",
    )
    assert result["provider"] == "media_storage"
    assert result["object_count"] == 3
    assert reader.objects == {}


@pytest.mark.asyncio
async def test_snapshot_blocks_unsupported_owned_avatar_reference(make_user):
    user = await make_user("GCS bad avatar")
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET avatar_url=:url WHERE id=:id"),
            {"url": "https://storage.googleapis.com/chirp-media/posts/foreign/avatar.jpg", "id": uuid.UUID(user.id)},
        )
        await session.commit()
        with pytest.raises(MediaProviderUnavailable, match="media_reference_scope_invalid"):
            await snapshot_user_media(
                session, uuid.UUID(user.id), FakeReader([]), freeze(user.id), bucket="chirp-media"
            )


@pytest.mark.asyncio
async def test_snapshot_blocks_foreign_reference_to_owned_object(make_user):
    owner = await make_user("GCS reference owner")
    foreign = await make_user("GCS reference foreign")
    url = f"https://storage.googleapis.com/chirp-media/avatars/{owner.id}/avatar.jpg"
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE users SET avatar_url=:url WHERE id IN (:owner, :foreign)"), {"url": url, "owner": uuid.UUID(owner.id), "foreign": uuid.UUID(foreign.id)})
        await session.commit()
        with pytest.raises(MediaProviderUnavailable, match="media_reference_foreign_owner"):
            await snapshot_user_media(
                session, uuid.UUID(owner.id), FakeReader([]), freeze(owner.id), bucket="chirp-media"
            )


@pytest.mark.asyncio
async def test_snapshot_drift_blocks_before_any_delete(make_user):
    user = await make_user("GCS drift")
    reader = FakeReader([(f"posts/{user.id}/post.jpg", "22")])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(user.id), reader, freeze(user.id), bucket="chirp-media"
        )
    reader.objects[f"avatars/{user.id}/new.jpg"] = "44"
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_drift"):
        await GCSMediaProvider(reader, bucket="chirp-media", snapshot=snapshot, freeze=freeze(user.id)).delete_account_data(
            user=user_model(user.id), idempotency_key="step-media",
        )
    assert not any(kind == "delete" for kind, _ in reader.calls)


@pytest.mark.asyncio
async def test_retry_accepts_already_absent_objects(make_user):
    user = await make_user("GCS retry")
    reader = FakeReader([(f"posts/{user.id}/post.jpg", "22")])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(user.id), reader, freeze(user.id), bucket="chirp-media"
        )
    provider = GCSMediaProvider(reader, bucket="chirp-media", snapshot=snapshot, freeze=freeze(user.id))
    await provider.delete_account_data(
        user=user_model(user.id), idempotency_key="step-media",
    )
    result = await provider.delete_account_data(
        user=user_model(user.id), idempotency_key="step-media",
    )
    assert result["readback"] is True
    assert [kind for kind, _ in reader.calls].count("delete") == 1


@pytest.mark.asyncio
async def test_foreign_listing_and_generation_change_fail_closed(make_user):
    user = await make_user("GCS foreign")
    reader = FakeReader([(f"posts/{user.id}/post.jpg", "22")])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(user.id), reader, freeze(user.id), bucket="chirp-media"
        )
    reader.objects[f"posts/{user.id}/post.jpg"] = "23"
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_drift"):
        await GCSMediaProvider(reader, bucket="chirp-media", snapshot=snapshot, freeze=freeze(user.id)).delete_account_data(
            user=user_model(user.id), idempotency_key="step-media",
        )
    assert not any(kind == "delete" for kind, _ in reader.calls)


def test_freeze_and_subject_binding_fail_before_reader_calls():
    reader = FakeReader([])
    snapshot = MediaSnapshot("owner", "chirp-media", ("tmp/", "posts/", "avatars/"), (), "freeze-c436", "digest")
    provider = GCSMediaProvider(reader, bucket="chirp-media", snapshot=snapshot, freeze=freeze("owner"))
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_subject_mismatch"):
        import asyncio
        asyncio.run(provider.delete_account_data(
            user=user_model(str(uuid.uuid4())), idempotency_key="step",
        ))
    assert reader.calls == []


def test_forged_snapshot_scope_digest_and_freeze_are_rejected():
    user_id = str(uuid.uuid4())
    reader = FakeReader([])
    foreign = empty_snapshot(user_id, objects=(MediaObject("posts/foreign/file.jpg", "1"),))
    provider = GCSMediaProvider(reader, bucket="chirp-media", snapshot=foreign, freeze=freeze(user_id))
    import asyncio
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_scope_invalid"):
        asyncio.run(provider.delete_account_data(user=user_model(user_id), idempotency_key="step"))
    invalid = empty_snapshot(user_id, digest="bad")
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_digest_invalid"):
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", snapshot=invalid, freeze=freeze(user_id)).delete_account_data(user=user_model(user_id), idempotency_key="step"))
    bad_freeze = FreezeReceipt(user_id, "chirp-media", "freeze-c436", time.time() + 300, verified="false")
    with pytest.raises(MediaProviderUnavailable, match="media_freeze_invalid"):
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", snapshot=empty_snapshot(user_id), freeze=bad_freeze).delete_account_data(user=user_model(user_id), idempotency_key="step"))


class InfiniteReader(FakeReader):
    def list(self, prefix: str, *, deadline: float):
        self.calls.append(("list", prefix))
        index = 0
        while True:
            index += 1
            yield SimpleNamespace(name=f"{prefix}item{index}.jpg", generation=str(index))


def test_infinite_listing_is_bounded_before_delete():
    user_id = str(uuid.uuid4())
    reader = InfiniteReader([])
    import asyncio
    with pytest.raises(MediaProviderUnavailable, match="media_object_cap_reached"):
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", snapshot=empty_snapshot(user_id), freeze=freeze(user_id)).delete_account_data(user=user_model(user_id), idempotency_key="step"))
    assert not any(kind == "delete" for kind, _ in reader.calls)


class SlowReader(FakeReader):
    def read(self, name: str, *, deadline: float):
        time.sleep(1.05)
        return super().read(name, deadline=deadline)


def test_reader_timeout_drains_inflight_operation_before_failure():
    user_id = str(uuid.uuid4())
    item = MediaObject(f"posts/{user_id}/file.jpg", "1")
    reader = SlowReader([(item.name, item.generation)])
    import asyncio
    started = time.monotonic()
    with pytest.raises(MediaProviderUnavailable, match="media_deadline_exceeded"):
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", snapshot=empty_snapshot(user_id, objects=(item,)), freeze=freeze(user_id)).delete_account_data(user=user_model(user_id), idempotency_key="step", deadline_seconds=1))
    assert time.monotonic() - started >= 1.0
    assert not any(kind == "delete" for kind, _ in reader.calls)


def test_gcs_reader_binds_list_reload_and_generation_delete(monkeypatch):
    class Blob:
        name = "posts/u/file.jpg"
        generation = 7
        size = 3
        md5_hash = None
        crc32c = None
        def reload(self, **kwargs):
            self.reload_kwargs = kwargs
        def delete(self, **kwargs):
            self.delete_kwargs = kwargs

    class Bucket:
        def __init__(self):
            self.blob_value = Blob()
            self.list_kwargs = None
        def list_blobs(self, **kwargs):
            self.list_kwargs = kwargs
            return [self.blob_value]
        def blob(self, name):
            self.blob_value.name = name
            return self.blob_value

    bucket = Bucket()
    monkeypatch.setattr("app.services.storage_service._storage_client", lambda: SimpleNamespace(bucket=lambda _: bucket))
    reader = GCSMediaReader("chirp-media")
    assert list(reader.list("posts/u/", deadline=time.monotonic() + 2))[0].name == "posts/u/file.jpg"
    assert bucket.list_kwargs["retry"] is None
    assert reader.read("posts/u/file.jpg", deadline=time.monotonic() + 2).generation == "7"
    reader.delete("posts/u/file.jpg", "7", deadline=time.monotonic() + 2)
    assert bucket.blob_value.delete_kwargs["if_generation_match"] == 7
