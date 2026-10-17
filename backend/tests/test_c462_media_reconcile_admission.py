"""Real-PostgreSQL account-hold admission for permanent media reconciliation."""
from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound
from google.api_core.exceptions import PreconditionFailed
from sqlalchemy import select

from app import models
from app.db import get_engine, get_session_factory
from app.jobs import media_reconcile
from app.services import storage_service
from app.services.account_deletion_hold import activate_deletion_hold
from app.services.account_external_operations import inspect_registered_operation_drain
from tests.conftest import MakeChapterWith

BUCKET = "chirps-prod-media"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)


async def _deletion_request(client, owner: object) -> uuid.UUID:
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _activate(owner_id: uuid.UUID, request_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=owner_id, request_id=request_id)
        await session.commit()


class _Blob:
    def __init__(self, name: str, *, generation: int = 71, delete=None) -> None:
        self.name = name
        self.generation = generation
        self.time_created = OLD
        self._delete = delete

    def delete(self, *, if_generation_match: int | None = None) -> None:
        assert if_generation_match == self.generation
        if self._delete is not None:
            self._delete()


class _Pages:
    def __init__(self, blobs: list[_Blob]) -> None:
        self.pages = iter([blobs])


def _fake_storage(monkeypatch: pytest.MonkeyPatch, *blobs: _Blob) -> None:
    client = SimpleNamespace(list_blobs=lambda bucket, prefix: _Pages(list(blobs)))
    monkeypatch.setattr(storage_service, "_storage_client", lambda: client)
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", BUCKET)


async def _run(**kwargs):
    async with get_session_factory()() as session:
        return await media_reconcile.reconcile_orphaned_media(
            session, now=NOW, **kwargs,
        )


async def _operation_rows(user_id: uuid.UUID) -> list[models.AccountExternalOperation]:
    async with get_session_factory()() as session:
        return list((await session.scalars(
            select(models.AccountExternalOperation).where(
                models.AccountExternalOperation.actor_user_id == user_id,
                models.AccountExternalOperation.kind == "media_reconcile_post",
            )
        )).all())


async def test_dry_run_stays_read_only_and_does_not_register_provider_work(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("member")
    name = f"posts/{setup.member.id}/dry-run.jpg"
    calls: list[str] = []
    _fake_storage(monkeypatch, _Blob(name, delete=lambda: calls.append(name)))

    result = await _run()

    assert result.eligible == (name,)
    assert calls == []
    assert await _operation_rows(uuid.UUID(setup.member.id)) == []


async def test_hold_activated_after_reference_snapshot_blocks_owner_but_allows_other_owner(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await make_chapter_with("member")
    other = await make_chapter_with("member")
    held_id, other_id = uuid.UUID(held.member.id), uuid.UUID(other.member.id)
    request_id = await _deletion_request(client, held.member)
    names = [f"posts/{held_id}/old.jpg", f"posts/{other_id}/old.jpg"]
    deleted: list[str] = []
    _fake_storage(monkeypatch, *(_Blob(name, delete=lambda n=name: deleted.append(n)) for name in names))

    collect = media_reconcile.collect_referenced_object_names
    activated = False

    async def snapshot_then_activate(session, bucket_name, *, list_eligible=False):
        nonlocal activated
        references = await collect(session, bucket_name, list_eligible=list_eligible)
        if not activated:
            activated = True
            await _activate(held_id, request_id)
        return references

    monkeypatch.setattr(media_reconcile, "collect_referenced_object_names", snapshot_then_activate)
    result = await _run(delete=True)

    assert activated
    assert result.eligible == tuple(names)
    assert result.deleted == (names[1],)
    assert deleted == [names[1]]
    assert await _operation_rows(held_id) == []
    other_rows = await _operation_rows(other_id)
    assert len(other_rows) == 1 and other_rows[0].state == "finished"
    drain = await inspect_registered_operation_drain(user_id=held_id, request_id=request_id)
    assert drain.registered_operations_drained


async def test_inflight_generation_delete_is_visible_to_hold_drain_until_finished(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("member")
    owner_id = uuid.UUID(setup.member.id)
    request_id = await _deletion_request(client, setup.member)
    entered, release = threading.Event(), threading.Event()
    observations: dict[str, object] = {}

    def delete() -> None:
        observations["checked_out"] = get_engine().sync_engine.pool.checkedout()
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test delete barrier timed out")

    name = f"posts/{owner_id}/inflight-generation.jpg"
    blob = _Blob(name, generation=8881, delete=delete)
    _fake_storage(monkeypatch, blob)
    task = asyncio.create_task(_run(delete=True))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # This hold wins after registration. Its final drain must observe the
        # registered GCS operation and wait until the exact generation call ends.
        await _activate(owner_id, request_id)
        drain = await inspect_registered_operation_drain(user_id=owner_id, request_id=request_id)
        assert (drain.running_count, drain.uncertain_count) == (1, 0)
        assert not drain.registered_operations_drained
        assert observations["checked_out"] == 0
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.deleted == (name,)
    rows = await _operation_rows(owner_id)
    assert len(rows) == 1 and rows[0].state == "finished"
    drain = await inspect_registered_operation_drain(user_id=owner_id, request_id=request_id)
    assert drain.registered_operations_drained
    assert blob.generation == 8881


async def test_ambiguous_gcs_failure_is_durable_and_never_replayed(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("member")
    owner_id = uuid.UUID(setup.member.id)
    calls: list[int] = []

    def fail_after_start() -> None:
        calls.append(1)
        raise RuntimeError("provider response was lost")

    name = f"posts/{owner_id}/ambiguous.jpg"
    blob = _Blob(name, generation=991, delete=fail_after_start)
    _fake_storage(monkeypatch, blob)
    with pytest.raises(RuntimeError, match="provider response was lost"):
        await _run(delete=True)

    rows = await _operation_rows(owner_id)
    assert len(rows) == 1 and rows[0].state == "uncertain"
    second = await _run(delete=True)
    assert second.deleted == ()
    assert calls == [1]
    rows = await _operation_rows(owner_id)
    assert len(rows) == 1 and rows[0].state == "uncertain"


async def test_not_found_is_a_finished_generation_bound_operation(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("member")
    owner_id = uuid.UUID(setup.member.id)
    name = f"posts/{owner_id}/gone.jpg"

    def missing() -> None:
        raise NotFound("generation no longer exists")

    _fake_storage(monkeypatch, _Blob(name, generation=177, delete=missing))
    result = await _run(delete=True)

    assert result.already_gone == (name,)
    rows = await _operation_rows(owner_id)
    expected = hashlib.sha256(
        f"media_reconcile_post\0{BUCKET}\0{name}\0{177}".encode("utf-8")
    ).hexdigest()
    assert len(rows) == 1 and rows[0].state == "finished"
    assert rows[0].effect_key == expected


async def test_replaced_generation_fails_precondition_and_is_not_counted_deleted(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("member")
    owner_id = uuid.UUID(setup.member.id)
    name = f"posts/{owner_id}/replaced.jpg"

    def replaced() -> None:
        raise PreconditionFailed("listed generation is no longer current")

    _fake_storage(monkeypatch, _Blob(name, generation=202, delete=replaced))
    result = await _run(delete=True)

    assert result.deleted == ()
    rows = await _operation_rows(owner_id)
    assert len(rows) == 1 and rows[0].state == "finished"
