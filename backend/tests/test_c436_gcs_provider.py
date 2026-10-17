"""Real database ownership guards and offline SDK account-media tests."""
from __future__ import annotations

import time
import asyncio
import threading
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
    MediaRetentionReview,
    snapshot_user_media,
)
from app.services.account_provider_gcs import _digest, PREFIXES


class FakeReader:
    bucket_name = "chirp-media"
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
    return FreezeReceipt(user_id, bucket, "freeze-c436", time.time() + 300, verified=True)


def reviewed_retention():
    return MediaRetentionReview("chirp-media", False, 604800, verified=True)


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
    result = await GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=snapshot, freeze=freeze(user.id)).delete_account_data(
        user=user_model(user.id), idempotency_key="step-media",
    )
    assert result["provider"] == "media_storage"
    assert result["object_count"] == 3
    assert result["retained_with_reason"] == ["gcs_soft_deleted_versions"]
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
        await GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=snapshot, freeze=freeze(user.id)).delete_account_data(
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
    provider = GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=snapshot, freeze=freeze(user.id))
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
        await GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=snapshot, freeze=freeze(user.id)).delete_account_data(
            user=user_model(user.id), idempotency_key="step-media",
        )
    assert not any(kind == "delete" for kind, _ in reader.calls)


def test_freeze_and_subject_binding_fail_before_reader_calls():
    reader = FakeReader([])
    snapshot = MediaSnapshot("owner", "chirp-media", ("tmp/", "posts/", "avatars/"), (), "freeze-c436", "digest")
    provider = GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=snapshot, freeze=freeze("owner"))
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
    provider = GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=foreign, freeze=freeze(user_id))
    import asyncio
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_scope_invalid"):
        asyncio.run(provider.delete_account_data(user=user_model(user_id), idempotency_key="step"))
    invalid = empty_snapshot(user_id, digest="bad")
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_digest_invalid"):
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=invalid, freeze=freeze(user_id)).delete_account_data(user=user_model(user_id), idempotency_key="step"))
    bad_freeze = FreezeReceipt(user_id, "chirp-media", "freeze-c436", time.time() + 300, verified="false")
    with pytest.raises(MediaProviderUnavailable, match="media_freeze_invalid"):
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=empty_snapshot(user_id), freeze=bad_freeze).delete_account_data(user=user_model(user_id), idempotency_key="step"))


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
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=empty_snapshot(user_id), freeze=freeze(user_id)).delete_account_data(user=user_model(user_id), idempotency_key="step"))
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
        asyncio.run(GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=empty_snapshot(user_id, objects=(item,)), freeze=freeze(user_id)).delete_account_data(user=user_model(user_id), idempotency_key="step", deadline_seconds=1))
    assert time.monotonic() - started >= 1.0
    assert not any(kind == "delete" for kind, _ in reader.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("url_form", ["canonical", "query", "encoded", "json", "console"])
async def test_foreign_orphan_reference_is_normalized_and_blocks_snapshot(make_user, url_form):
    owner = await make_user("Media orphan owner")
    foreign = await make_user("Media orphan foreign")
    name = f"avatars/{owner.id}/orphan.jpg"
    from urllib.parse import quote
    urls = {
        "canonical": f"https://storage.googleapis.com/chirp-media/{name}",
        "query": f"https://storage.googleapis.com/chirp-media/{name}?alt=media",
        "encoded": f"https://storage.googleapis.com/chirp-media/{quote(name, safe='')}",
        "json": f"https://storage.googleapis.com/storage/v1/b/chirp-media/o/{quote(name, safe='')}?alt=media",
        "console": f"https://storage.cloud.google.com/chirp-media/{name}",
    }
    reader = FakeReader([(name, "1")])
    async with get_session_factory()() as session:
        # Owner has no current reference: its historical orphan is still shared.
        await session.execute(text("UPDATE users SET avatar_url=:url WHERE id=:id"),
                              {"url": urls[url_form], "id": uuid.UUID(foreign.id)})
        await session.commit()
        with pytest.raises(MediaProviderUnavailable, match="media_reference_foreign_owner"):
            await snapshot_user_media(session, uuid.UUID(owner.id), reader, freeze(owner.id), bucket="chirp-media")
    assert reader.calls == []


@pytest.mark.asyncio
async def test_aggregate_cap_across_prefixes_is_not_per_prefix(make_user):
    owner = await make_user("Media cap owner")
    reader = FakeReader([(f"{prefix}{owner.id}/item.jpg", "1") for prefix in PREFIXES])
    async with get_session_factory()() as session:
        with pytest.raises(MediaProviderUnavailable, match="media_object_cap_reached"):
            await snapshot_user_media(session, uuid.UUID(owner.id), reader, freeze(owner.id), bucket="chirp-media", max_objects=2)
    assert not any(kind == "delete" for kind, _ in reader.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_mutating_worker_is_drained_before_timeout_or_cancellation_returns(cancel):
    from app.services.account_provider_gcs import _reader_call
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Reader:
        def delete(self, name, generation, *, deadline):
            entered.set()
            release.wait(2)
            finished.set()
    task = asyncio.create_task(_reader_call(Reader(), "delete", "owned", "1", deadline=time.monotonic() + .1))
    assert await asyncio.to_thread(entered.wait, 1)
    if cancel:
        task.cancel()
    await asyncio.sleep(.15)
    assert not task.done(), "must not abandon a still-mutating worker"
    if cancel:
        task.cancel()  # repeated cancellation must also keep the worker attached
        await asyncio.sleep(.01)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError if cancel else MediaProviderUnavailable):
        await task
    assert finished.is_set()


def test_actual_gcs_sdk_pagination_and_generation_preconditions(monkeypatch):
    from google.cloud import storage
    from google.auth.credentials import AnonymousCredentials
    from google.api_core.exceptions import NotFound, PreconditionFailed, Forbidden
    from app.services import account_provider_gcs as module
    monkeypatch.setenv(module.DISABLE_BUCKET_METADATA, "true")
    client = storage.Client(project="offline-fixture", credentials=AnonymousCredentials())
    calls = []
    name = "posts/00000000-0000-0000-0000-000000000001/a.jpg"
    def api_request(**kwargs):
        calls.append(kwargs)
        if kwargs["path"].endswith("/o"):
            if kwargs.get("query_params", {}).get("pageToken"):
                return {"items": [{"name": name.replace("a.jpg", "b.jpg"), "generation": "8"}]}
            return {"items": [{"name": name, "generation": "7"}], "nextPageToken": "page-2"}
        if kwargs["method"] == "DELETE":
            return None
        return {"name": name, "generation": "7", "size": "3"}
    monkeypatch.setattr(client._connection, "api_request", api_request)
    created = []
    def selected_client(**kwargs):
        created.append(kwargs)
        return client
    monkeypatch.setattr(storage, "Client", selected_client)
    background_starts = []
    def unexpected_background_start(self):
        background_starts.append(self)
        raise AssertionError("SDK must not start background metadata requests")
    monkeypatch.setattr(threading.Thread, "start", unexpected_background_start)
    reader = GCSMediaReader("chirp-media", project="offline-fixture", credentials=client._credentials)
    assert created == [{"project": "offline-fixture", "credentials": client._credentials}]
    deadlines = iter([20.0, 10.0])
    monkeypatch.setattr(module, "_remaining", lambda deadline: next(deadlines))
    listed = list(reader.list("posts/", deadline=100))
    assert [(x.name, x.generation) for x in listed] == [(name, 7), (name.replace("a.jpg", "b.jpg"), 8)]
    assert [c["timeout"] for c in calls] == [20.0, 10.0]
    assert calls[1]["query_params"]["pageToken"] == "page-2"
    assert all(c["retry"] is None and c["query_params"]["maxResults"] == 1000 for c in calls)
    monkeypatch.setattr(module, "_remaining", lambda deadline: 3.0)
    assert reader.read(name, deadline=100).generation == "7"
    reader.delete(name, "7", deadline=100)
    assert calls[-1]["query_params"]["ifGenerationMatch"] == 7
    assert calls[-1]["retry"] is None
    def raise_error(error):
        def fail(**kwargs):
            raise error("private-provider-sentinel")
        monkeypatch.setattr(client._connection, "api_request", fail)
    raise_error(NotFound)
    assert reader.read(name, deadline=100) is None
    raise_error(PreconditionFailed)
    with pytest.raises(MediaProviderUnavailable, match="^media_generation_mismatch$"):
        reader.delete(name, "7", deadline=100)
    raise_error(Forbidden)
    with pytest.raises(MediaProviderUnavailable, match="^media_read_failed$"):
        reader.read(name, deadline=100)
    assert background_starts == []


def test_sdk_requires_background_metadata_opt_out_before_client_creation(monkeypatch):
    from google.cloud import storage
    from app.services import account_provider_gcs as module
    monkeypatch.delenv(module.DISABLE_BUCKET_METADATA, raising=False)
    created = []
    monkeypatch.setattr(storage, "Client", lambda: created.append(True))
    with pytest.raises(MediaProviderUnavailable, match="^media_sdk_background_requests_enabled$"):
        GCSMediaReader("chirp-media")
    assert created == []


@pytest.mark.asyncio
async def test_reader_bucket_mismatch_blocks_before_any_provider_call(make_user):
    user = await make_user("Wrong reader bucket")
    reader = FakeReader([])
    reader.bucket_name = "different-bucket"
    async with get_session_factory()() as session:
        with pytest.raises(MediaProviderUnavailable, match="^media_reader_bucket_mismatch$"):
            await snapshot_user_media(session, uuid.UUID(user.id), reader, freeze(user.id), bucket="chirp-media")
    with pytest.raises(MediaProviderUnavailable, match="^media_reader_bucket_mismatch$"):
        GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=empty_snapshot(user.id), freeze=freeze(user.id))
    assert reader.calls == []


@pytest.mark.asyncio
async def test_reappeared_earlier_object_blocks_final_success():
    owner = str(uuid.uuid4())
    first, second = f"posts/{owner}/a.jpg", f"posts/{owner}/b.jpg"
    class ReappearingReader(FakeReader):
        def delete(self, name, generation, *, deadline):
            super().delete(name, generation, deadline=deadline)
            if name == second:
                self.objects[first] = "99"
    reader = ReappearingReader([(first, "1"), (second, "2")])
    snapshot = empty_snapshot(owner, objects=(MediaObject(first, "1"), MediaObject(second, "2")))
    with pytest.raises(MediaProviderUnavailable, match="media_reappeared"):
        await GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=snapshot, freeze=freeze(owner)).delete_account_data(user=user_model(owner), idempotency_key="step")
    assert reader.objects == {first: "99"}


@pytest.mark.asyncio
async def test_expired_freeze_after_read_blocks_delete():
    owner = str(uuid.uuid4())
    item = MediaObject(f"posts/{owner}/a.jpg", "1")
    now = [time.time()]
    class Reader(FakeReader):
        def read(self, name, *, deadline):
            result = super().read(name, deadline=deadline)
            if len([call for call in self.calls if call[0] == "read"]) == 2:
                now[0] += 301
            return result
    reader = Reader([(item.name, item.generation)])
    from unittest.mock import patch
    frozen = freeze(owner)
    with patch("app.services.account_provider_gcs.time.time", side_effect=lambda: now[0]):
        with pytest.raises(MediaProviderUnavailable, match="media_freeze_invalid"):
            await GCSMediaProvider(reader, bucket="chirp-media", retention=reviewed_retention(), snapshot=empty_snapshot(owner, objects=(item,)), freeze=frozen).delete_account_data(user=user_model(owner), idempotency_key="step")
    assert not any(kind == "delete" for kind, _ in reader.calls)


@pytest.mark.parametrize("retention", [
    None,
    MediaRetentionReview("chirp-media", False, 604800),
    MediaRetentionReview("other-bucket", False, 604800, verified=True),
    MediaRetentionReview("chirp-media", True, 604800, verified=True),
    MediaRetentionReview("chirp-media", None, 604800, verified=True),
    MediaRetentionReview("chirp-media", False, None, verified=True),
    MediaRetentionReview("chirp-media", False, True, verified=True),
])
def test_retention_review_required_before_provider_calls(retention):
    owner = str(uuid.uuid4())
    reader = FakeReader([])
    with pytest.raises(MediaProviderUnavailable, match="^media_retention_review_required$"):
        GCSMediaProvider(reader, bucket="chirp-media", snapshot=empty_snapshot(owner), freeze=freeze(owner), retention=retention)
    assert reader.calls == []


@pytest.mark.asyncio
async def test_reviewed_no_versioning_or_soft_delete_policy_has_no_provider_retention():
    owner = str(uuid.uuid4())
    result = await GCSMediaProvider(
        FakeReader([]), bucket="chirp-media", snapshot=empty_snapshot(owner), freeze=freeze(owner),
        retention=MediaRetentionReview("chirp-media", False, 0, verified=True),
    ).delete_account_data(user=user_model(owner), idempotency_key="step")
    assert result["retained_with_reason"] == []
