"""Named operations must not expose or erase an anonymous block (c342)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import MakeCampus, MakeChapterWith, MakeUser, set_campus
from tests.test_c279_block_provenance import _chirp_ids, _named_surfaces, _scene


def _start_block_operations_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold both requests at the block SQL boundary before letting either execute."""
    execute = AsyncSession.execute
    ready = asyncio.Event()
    arrived = 0

    async def synchronized(
        session: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal arrived
        is_block_insert = (
            getattr(statement, "is_insert", False)
            and statement.table.name == "user_blocks"
        )
        is_block_read = getattr(statement, "is_select", False) and any(
            getattr(table, "name", None) == "user_blocks"
            for table in statement.get_final_froms()
        )
        if is_block_insert or is_block_read:
            arrived += 1
            if arrived >= 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=10)
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", synchronized)


async def test_named_delete_cannot_distinguish_or_remove_anonymous_only_block(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    scene = await _scene(client, make_chapter_with)
    innocent = await make_user("An innocent candidate")
    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text
    before = await _named_surfaces(client, scene)

    observations = []
    for candidate in (innocent, scene.author):
        response = await client.delete(
            "/moderation/blocks",
            params={"blocked_id": candidate.id},
            headers=scene.blocker.headers,
        )
        observations.append((response.status_code, response.text))
        assert scene.chirp_id not in await _chirp_ids(client, scene)
        assert await _named_surfaces(client, scene) == before
    assert observations == [(404, '{"detail":"block_not_found"}')] * 2


async def test_named_create_does_not_return_anonymous_block_timestamp(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    scene = await _scene(client, make_chapter_with)
    innocent = await make_user("Another innocent candidate")
    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text
    from app.db import get_session_factory

    old_time = datetime.now(timezone.utc) - timedelta(days=7)
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE user_blocks SET created_at = :created_at WHERE blocker_id = :id"),
            {"created_at": old_time, "id": scene.blocker.id},
        )
        await session.commit()

    started = datetime.now(timezone.utc)
    for candidate in (innocent, scene.author):
        response = await client.post(
            "/moderation/blocks",
            json={"blocked_id": candidate.id},
            headers=scene.blocker.headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert set(body) == {"blocker_id", "blocked_id", "created_at"}
        assert body["blocked_id"] == candidate.id
        assert started <= datetime.fromisoformat(body["created_at"]) <= datetime.now(timezone.utc)


async def test_named_cycles_preserve_anonymous_intent_and_recipient_safety(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    scene = await _scene(client, make_chapter_with)
    innocent = await make_user("Candidate with their own Chirp")
    await set_campus(innocent.id, scene.campus_id)
    innocent_chirp = await client.post(
        f"/campuses/{scene.campus_id}/chirps",
        json={"body": "an unrelated anonymous post"},
        headers=innocent.headers,
    )
    assert innocent_chirp.status_code == 201, innocent_chirp.text
    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text
    named_before = await _named_surfaces(client, scene)

    for candidate in (innocent, scene.author, scene.author):
        created = await client.post(
            "/moderation/blocks",
            json={"blocked_id": candidate.id},
            headers=scene.blocker.headers,
        )
        assert created.status_code == 201, created.text
        assert set(await _chirp_ids(client, scene)) == {innocent_chirp.json()["id"]}
        deleted = await client.delete(
            "/moderation/blocks",
            params={"blocked_id": candidate.id},
            headers=scene.blocker.headers,
        )
        assert deleted.status_code == 204, deleted.text
        assert set(await _chirp_ids(client, scene)) == {innocent_chirp.json()["id"]}
        assert await _named_surfaces(client, scene) == named_before

    incoming = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [scene.blocker.id]},
        headers=scene.author.headers,
    )
    assert incoming.status_code == 403, incoming.text
    assert incoming.json() == {"detail": "recipient_not_reachable"}
    outgoing = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [scene.author.id]},
        headers=scene.blocker.headers,
    )
    assert outgoing.status_code == 201, outgoing.text


async def test_named_actions_never_change_chirps_even_during_anonymous_undo(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    scene = await _scene(client, make_chapter_with)
    before = await _chirp_ids(client, scene)
    named = await client.post(
        "/moderation/blocks", json={"blocked_id": scene.author.id}, headers=scene.blocker.headers
    )
    assert named.status_code == 201, named.text
    assert await _chirp_ids(client, scene) == before
    named_surfaces = await _named_surfaces(client, scene)

    anonymous = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert anonymous.status_code == 204, anonymous.text
    assert scene.chirp_id not in await _chirp_ids(client, scene)
    for _ in range(2):
        undo = await client.delete(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
        )
        assert undo.status_code == 204 and undo.content == b"", undo.text
        assert await _chirp_ids(client, scene) == before
        assert await _named_surfaces(client, scene) == named_surfaces

    # Anonymous undo cannot weaken a named recipient block.
    incoming = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [scene.blocker.id]},
        headers=scene.author.headers,
    )
    assert incoming.status_code == 403, incoming.text
    duplicate = await client.post(
        "/moderation/blocks", json={"blocked_id": scene.author.id}, headers=scene.blocker.headers
    )
    assert duplicate.status_code == 409, duplicate.text


async def test_anonymous_undo_restores_visibility_and_contact_without_a_named_intent(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    scene = await _scene(client, make_chapter_with)
    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text
    for _ in range(2):
        undo = await client.delete(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
        )
        assert undo.status_code == 204 and undo.content == b"", undo.text
    assert scene.chirp_id in await _chirp_ids(client, scene)
    incoming = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [scene.blocker.id]},
        headers=scene.author.headers,
    )
    assert incoming.status_code == 201, incoming.text


async def test_anonymous_undo_keeps_campus_and_self_guards(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    make_user: MakeUser, make_campus: MakeCampus,
) -> None:
    scene = await _scene(client, make_chapter_with)
    outsider = await make_user("Other campus")
    await set_campus(outsider.id, await make_campus())
    unverified = await make_user("No verified campus")
    await set_campus(unverified.id, scene.campus_id, verified=False)
    for caller, detail in (
        (outsider, "not_your_campus"),
        (unverified, "campus_unverified"),
        (scene.author, "cannot_block_self"),
    ):
        response = await client.delete(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=caller.headers
        )
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": detail}
    missing = await client.delete(
        f"/moderation/blocks/by-chirp/{uuid.uuid4()}", headers=scene.blocker.headers
    )
    assert missing.status_code == 404, missing.text
    unauthenticated = await client.delete(f"/moderation/blocks/by-chirp/{scene.chirp_id}")
    assert unauthenticated.status_code == 401, unauthenticated.text


async def test_removed_but_retained_chirp_can_undo_anonymous_intent(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    scene = await _scene(client, make_chapter_with)
    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text
    removed = await client.delete(f"/chirps/{scene.chirp_id}", headers=scene.author.headers)
    assert removed.status_code == 204, removed.text
    undo = await client.delete(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert undo.status_code == 204 and undo.content == b"", undo.text
    incoming = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [scene.blocker.id]},
        headers=scene.author.headers,
    )
    assert incoming.status_code == 201, incoming.text


@pytest.mark.parametrize("prior_anonymous", [False, True])
async def test_concurrent_named_creates_have_one_winner_with_or_without_anonymous_intent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, prior_anonymous: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = await _scene(client, make_chapter_with)
    if prior_anonymous:
        response = await client.post(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
        )
        assert response.status_code == 204, response.text
    _start_block_operations_together(monkeypatch)
    responses = await asyncio.gather(*(
        client.post(
            "/moderation/blocks", json={"blocked_id": scene.author.id},
            headers=scene.blocker.headers,
        )
        for _ in range(2)
    ))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert (scene.chirp_id not in await _chirp_ids(client, scene)) == prior_anonymous


async def test_concurrent_named_and_anonymous_create_retain_both_intents(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = await _scene(client, make_chapter_with)
    _start_block_operations_together(monkeypatch)
    named, anonymous = await asyncio.gather(
        client.post(
            "/moderation/blocks", json={"blocked_id": scene.author.id},
            headers=scene.blocker.headers,
        ),
        client.post(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
        ),
    )
    assert named.status_code == 201, named.text
    assert anonymous.status_code == 204, anonymous.text
    assert scene.author.id not in (await _named_surfaces(client, scene))["posts"]
    assert scene.chirp_id not in await _chirp_ids(client, scene)
    deleted = await client.delete(
        "/moderation/blocks", params={"blocked_id": scene.author.id}, headers=scene.blocker.headers
    )
    assert deleted.status_code == 204, deleted.text
    assert scene.chirp_id not in await _chirp_ids(client, scene)


async def test_concurrent_named_delete_and_anonymous_create_retain_anonymous_intent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = await _scene(client, make_chapter_with)
    named = await client.post(
        "/moderation/blocks", json={"blocked_id": scene.author.id}, headers=scene.blocker.headers
    )
    assert named.status_code == 201, named.text
    _start_block_operations_together(monkeypatch)
    deleted, anonymous = await asyncio.gather(
        client.delete(
            "/moderation/blocks", params={"blocked_id": scene.author.id},
            headers=scene.blocker.headers,
        ),
        client.post(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
        ),
    )
    assert deleted.status_code == 204, deleted.text
    assert anonymous.status_code == 204, anonymous.text
    assert scene.chirp_id not in await _chirp_ids(client, scene)
    assert scene.author.id in (await _named_surfaces(client, scene))["posts"]


async def test_concurrent_anonymous_undo_and_named_create_retain_named_intent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = await _scene(client, make_chapter_with)
    anonymous = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert anonymous.status_code == 204, anonymous.text
    _start_block_operations_together(monkeypatch)
    deleted, named = await asyncio.gather(
        client.delete(
            f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
        ),
        client.post(
            "/moderation/blocks", json={"blocked_id": scene.author.id},
            headers=scene.blocker.headers,
        ),
    )
    assert deleted.status_code == 204, deleted.text
    assert named.status_code == 201, named.text
    assert scene.chirp_id in await _chirp_ids(client, scene)
    assert scene.author.id not in (await _named_surfaces(client, scene))["posts"]
