"""Deletion-hold admission for retained lineage history and edges."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import DeletionHoldInvalid
from app.schemas.lineage import LineageEdgeCreate
from tests.conftest import MakeChapterWith, MakeUser


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key=f"lineage-{uuid.uuid4()}"
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


async def _join(client: AsyncClient, chapter_id: str, president: Any, user: Any) -> None:
    invite = await client.post(
        f"/chapters/{chapter_id}/invites",
        json={"role": "member"},
        headers=president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=user.headers
    )
    assert joined.status_code == 201, joined.text


async def _edge(
    client: AsyncClient, chapter_id: str, actor: Any, big: str, little: str,
    *, replace_existing: bool = False,
) -> Any:
    return await client.post(
        f"/chapters/{chapter_id}/lineage/edges",
        json={
            "big_user_id": big,
            "little_user_id": little,
            "replace_existing": replace_existing,
        },
        headers=actor.headers,
    )


async def _edge_rows(chapter_id: str) -> list[models.LineageEdge]:
    async with get_session_factory()() as session:
        return list((await session.scalars(
            select(models.LineageEdge).where(
                models.LineageEdge.chapter_id == uuid.UUID(chapter_id)
            )
        )).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("held_side", ["actor", "big", "little"])
async def test_held_actor_or_endpoint_cannot_create_lineage_edge(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch, held_side: str,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    big, little = await make_user("lineage big"), await make_user("lineage little")
    await _join(client, setup.chapter_id, setup.president, big)
    await _join(client, setup.chapter_id, setup.president, little)
    held = {"actor": setup.president, "big": big, "little": little}[held_side]
    await _hold(held.id)

    response = await _edge(client, setup.chapter_id, setup.president, big.id, little.id)
    assert response.status_code == 423, response.text
    assert await _edge_rows(setup.chapter_id) == []


@pytest.mark.asyncio
async def test_held_little_cannot_be_reassigned_and_old_edge_survives(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    big_a, big_b, little = [await make_user(name) for name in ("big a", "big b", "little")]
    for user in (big_a, big_b, little):
        await _join(client, setup.chapter_id, setup.president, user)
    first = await _edge(client, setup.chapter_id, setup.president, big_a.id, little.id)
    assert first.status_code == 201, first.text
    old_id = first.json()["id"]
    await _hold(little.id)

    replacement = await _edge(
        client, setup.chapter_id, setup.president, big_b.id, little.id,
        replace_existing=True,
    )
    assert replacement.status_code == 423, replacement.text
    rows = await _edge_rows(setup.chapter_id)
    assert len(rows) == 1 and str(rows[0].id) == old_id
    assert str(rows[0].big_user_id) == big_a.id


@pytest.mark.asyncio
async def test_unauthorized_officer_does_not_learn_target_hold(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    target = await make_user("held lineage target")
    await _join(client, setup.chapter_id, setup.president, target)
    await _hold(target.id)

    response = await _edge(
        client, setup.chapter_id, setup.member, setup.president.id, target.id
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


@pytest.mark.asyncio
async def test_role_loss_after_preflight_prevents_edge_write(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    big, little = await make_user("role loss big"), await make_user("role loss little")
    await _join(client, setup.chapter_id, setup.president, big)
    await _join(client, setup.chapter_id, setup.president, little)

    from app.services import lineage_write_admission

    original = lineage_write_admission._authorized_actor
    calls = 0

    async def lose_role(session: Any, *, uid: str, chapter_id: uuid.UUID):
        nonlocal calls
        calls += 1
        if calls == 2:
            async with get_session_factory()() as mutation:
                await mutation.execute(
                    text(
                        "UPDATE memberships SET role = 'member' "
                        "WHERE chapter_id = :chapter AND user_id = :user"
                    ),
                    {"chapter": chapter_id, "user": uuid.UUID(setup.president.id)},
                )
                await mutation.commit()
        return await original(session, uid=uid, chapter_id=chapter_id)

    monkeypatch.setattr(lineage_write_admission, "_authorized_actor", lose_role)
    response = await _edge(client, setup.chapter_id, setup.president, big.id, little.id)
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}
    assert await _edge_rows(setup.chapter_id) == []


@pytest.mark.asyncio
async def test_changed_body_targets_fail_against_bound_scope(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import lineage_write_admission

    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    big, little, changed = [
        await make_user(name) for name in ("drift big", "drift little", "drift target")
    ]
    for user in (big, little, changed):
        await _join(client, setup.chapter_id, setup.president, user)
    body = LineageEdgeCreate(
        big_user_id=uuid.UUID(big.id), little_user_id=uuid.UUID(little.id)
    )
    calls = 0

    original = lineage_write_admission._authorized_actor

    async def mutate_body_after_reauthorization(
        session: Any, *, uid: str, chapter_id: uuid.UUID
    ):
        nonlocal calls
        result = await original(session, uid=uid, chapter_id=chapter_id)
        calls += 1
        if calls == 2:
            body.little_user_id = uuid.UUID(changed.id)
        return result

    monkeypatch.setattr(
        lineage_write_admission, "_authorized_actor", mutate_body_after_reauthorization
    )
    async with get_session_factory()() as session:
        with pytest.raises(DeletionHoldInvalid, match="write_guard_target_set_changed"):
            await lineage_write_admission.get_lineage_edge_writer(
                chapter_id=uuid.UUID(setup.chapter_id),
                body=body,
                uid=setup.president.firebase_uid,
                session=session,
            )
        await session.rollback()
    assert await _edge_rows(setup.chapter_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target_state", ["removed", "completed"])
async def test_completed_tombstone_cannot_be_relinked(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch, target_state: str,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    target = await make_user("tombstoned lineage target")
    await _join(client, setup.chapter_id, setup.president, target)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(target.id))
        if target_state == "completed":
            # Represent the durable local tombstone fields from finalization; this
            # fixture does not claim that the external provider steps completed.
            await session.execute(
                text("UPDATE users SET is_ghost = true, firebase_uid = :uid WHERE id = :id"),
                {"uid": f"deleted:{target.id}", "id": uuid.UUID(target.id)},
            )
        await session.commit()

    response = await _edge(
        client, setup.chapter_id, setup.president, setup.president.id, target.id
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "lineage_target_not_in_chapter"}
    assert await _edge_rows(setup.chapter_id) == []


@pytest.mark.asyncio
async def test_cleanup_retains_lineage_edge_and_officer_can_remove_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    target = await make_user("retained lineage target")
    await _join(client, setup.chapter_id, setup.president, target)
    created = await _edge(
        client, setup.chapter_id, setup.president, setup.president.id, target.id
    )
    assert created.status_code == 201, created.text
    edge_id = created.json()["id"]
    await _hold(target.id)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(target.id))
        await session.commit()

    rows = await _edge_rows(setup.chapter_id)
    assert len(rows) == 1 and str(rows[0].id) == edge_id
    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}",
        headers=setup.president.headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert await _edge_rows(setup.chapter_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["family", "delete", "confirm"])
async def test_held_lineage_writer_cannot_mutate_history(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    little = await make_user("held lineage writer")
    await _join(client, setup.chapter_id, setup.president, little)
    created = await _edge(
        client, setup.chapter_id, setup.president, setup.president.id, little.id
    )
    assert created.status_code == 201, created.text
    edge_id = created.json()["id"]
    actor = little if operation == "confirm" else setup.president
    await _hold(actor.id)

    if operation == "family":
        response = await client.post(
            f"/chapters/{setup.chapter_id}/lineage/families",
            json={"name": "Held family", "color": "#6366f1"},
            headers=actor.headers,
        )
    elif operation == "delete":
        response = await client.delete(
            f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}",
            headers=actor.headers,
        )
    else:
        response = await client.post(
            f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}/confirm",
            headers=actor.headers,
        )
    assert response.status_code == 423, response.text
    rows = await _edge_rows(setup.chapter_id)
    assert len(rows) == 1 and str(rows[0].id) == edge_id
    assert rows[0].confirmed_by_little is False


@pytest.mark.asyncio
async def test_flag_off_preserves_lineage_write_behavior_for_held_actor(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("member")
    await _hold(setup.president.id)
    family = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/families",
        json={"name": "Still available", "color": "#6366f1"},
        headers=setup.president.headers,
    )
    assert family.status_code == 201, family.text
    edge = await _edge(
        client, setup.chapter_id, setup.president, setup.president.id, setup.member.id
    )
    assert edge.status_code == 201, edge.text
    async with get_session_factory()() as session:
        count = await session.scalar(
            select(func.count()).select_from(models.Family).where(
                models.Family.chapter_id == uuid.UUID(setup.chapter_id)
            )
        )
    assert count == 1
    assert len(await _edge_rows(setup.chapter_id)) == 1
