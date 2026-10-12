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
    MediaProviderUnavailable,
    MediaSnapshot,
    snapshot_user_media,
)


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
    result = await GCSMediaProvider(reader, bucket="chirp-media").delete_account_data(
        user=user_model(user.id), idempotency_key="step-media", snapshot=snapshot,
        freeze=freeze(user.id),
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
async def test_snapshot_drift_blocks_before_any_delete(make_user):
    user = await make_user("GCS drift")
    reader = FakeReader([(f"posts/{user.id}/post.jpg", "22")])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(user.id), reader, freeze(user.id), bucket="chirp-media"
        )
    reader.objects[f"avatars/{user.id}/new.jpg"] = "44"
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_drift"):
        await GCSMediaProvider(reader, bucket="chirp-media").delete_account_data(
            user=user_model(user.id), idempotency_key="step-media", snapshot=snapshot,
            freeze=freeze(user.id),
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
    provider = GCSMediaProvider(reader, bucket="chirp-media")
    await provider.delete_account_data(
        user=user_model(user.id), idempotency_key="step-media", snapshot=snapshot,
        freeze=freeze(user.id),
    )
    result = await provider.delete_account_data(
        user=user_model(user.id), idempotency_key="step-media", snapshot=snapshot,
        freeze=freeze(user.id),
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
        await GCSMediaProvider(reader, bucket="chirp-media").delete_account_data(
            user=user_model(user.id), idempotency_key="step-media", snapshot=snapshot,
            freeze=freeze(user.id),
        )
    assert not any(kind == "delete" for kind, _ in reader.calls)


def test_freeze_and_subject_binding_fail_before_reader_calls():
    reader = FakeReader([])
    snapshot = MediaSnapshot("owner", "chirp-media", ("tmp/", "posts/", "avatars/"), (), "freeze-c436", "digest")
    provider = GCSMediaProvider(reader, bucket="chirp-media")
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_subject_mismatch"):
        import asyncio
        asyncio.run(provider.delete_account_data(
            user=user_model(str(uuid.uuid4())), idempotency_key="step", snapshot=snapshot,
            freeze=freeze("owner"),
        ))
    assert reader.calls == []

