"""Lineage: one-big-per-little, confirm flow, and tree payload shape."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeChapterWith, MakeUser


async def _set_ghost(user_id: str) -> None:
    """Mark a fixture user as a historical lineage placeholder."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_ghost = true WHERE id = :user_id"),
            {"user_id": user_id},
        )
        await session.commit()


async def _set_membership_status(user_id: str, chapter_id: str, status: str) -> None:
    """Set a fixture membership status for active/inactive authorization checks."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE memberships SET status = :status "
                "WHERE user_id = :user_id AND chapter_id = :chapter_id"
            ),
            {"status": status, "user_id": user_id, "chapter_id": chapter_id},
        )
        await session.commit()


async def _create_family(client: AsyncClient, chapter_id: str, headers: dict, name: str) -> str:
    response = await client.post(
        f"/chapters/{chapter_id}/lineage/families",
        json={"name": name, "color": "#6366f1"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_little_may_have_only_one_big(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("historian")
    big_a = await make_user("Big A")
    big_b = await make_user("Big B")
    little = await make_user("Little One")

    # Active memberships for all three so they appear in the tree.
    for user in (big_a, big_b, little):
        invite = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert invite.status_code == 201, invite.text
        joined = await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=user.headers,
        )
        assert joined.status_code == 201, joined.text

    family_id = await _create_family(
        client, setup.chapter_id, setup.member.headers, "Hammer"
    )

    first = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={
            "big_user_id": big_a.id,
            "little_user_id": little.id,
            "family_id": family_id,
        },
        headers=setup.member.headers,
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={
            "big_user_id": big_b.id,
            "little_user_id": little.id,
            "family_id": family_id,
        },
        headers=setup.member.headers,
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "little_already_has_big"


@pytest.mark.asyncio
async def test_little_confirms_own_edge(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("historian")
    big = await make_user("Big")
    little = await make_user("Little")

    for user in (big, little):
        invite = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert invite.status_code == 201
        joined = await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=user.headers,
        )
        assert joined.status_code == 201

    family_id = await _create_family(client, setup.chapter_id, setup.member.headers, "Anchor")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={
            "big_user_id": big.id,
            "little_user_id": little.id,
            "family_id": family_id,
            "pledge_class": "Fall 2026",
        },
        headers=setup.member.headers,
    )
    assert created.status_code == 201
    edge_id = created.json()["id"]
    assert created.json()["confirmed_by_little"] is False

    denied = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}/confirm",
        headers=big.headers,
    )
    assert denied.status_code == 403

    confirmed = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}/confirm",
        headers=little.headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_by_little"] is True


@pytest.mark.asyncio
async def test_lineage_tree_includes_depth_and_propagates_family(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("historian")
    root = await make_user("Root Big")
    mid = await make_user("Mid")
    tip = await make_user("Tip Little")

    for user in (root, mid, tip):
        invite = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert invite.status_code == 201
        joined = await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=user.headers,
        )
        assert joined.status_code == 201

    family_id = await _create_family(client, setup.chapter_id, setup.member.headers, "Pulse")
    for big_id, little_id in ((root.id, mid.id), (mid.id, tip.id)):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/lineage/edges",
            json={
                "big_user_id": big_id,
                "little_user_id": little_id,
                "family_id": family_id,
            },
            headers=setup.member.headers,
        )
        assert response.status_code == 201, response.text

    tree = await client.get(
        f"/chapters/{setup.chapter_id}/lineage",
        headers=setup.member.headers,
    )
    assert tree.status_code == 200, tree.text
    payload = tree.json()
    by_id = {node["user_id"]: node for node in payload["nodes"]}
    assert by_id[root.id]["depth"] == 0
    assert by_id[mid.id]["depth"] == 1
    assert by_id[tip.id]["depth"] == 2
    assert by_id[root.id]["family_id"] == family_id
    assert by_id[tip.id]["family_id"] == family_id
    assert len(payload["edges"]) == 2
    assert len(payload["families"]) == 1


@pytest.mark.asyncio
async def test_rejects_self_edge_and_cycle(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("historian")
    a = await make_user("A")
    b = await make_user("B")
    for user in (a, b):
        invite = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert invite.status_code == 201
        joined = await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=user.headers,
        )
        assert joined.status_code == 201

    self_edge = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": a.id, "little_user_id": a.id},
        headers=setup.member.headers,
    )
    assert self_edge.status_code == 422

    first = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": a.id, "little_user_id": b.id},
        headers=setup.member.headers,
    )
    assert first.status_code == 201

    cycle = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": b.id, "little_user_id": a.id},
        headers=setup.member.headers,
    )
    assert cycle.status_code == 422
    assert cycle.json()["detail"] == "lineage_cycle"


async def _join_as_member(client: AsyncClient, setup, user) -> None:
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=user.headers
    )
    assert joined.status_code == 201, joined.text


async def _edges_for(client: AsyncClient, setup, little_id: str) -> list[dict]:
    tree = await client.get(f"/chapters/{setup.chapter_id}/lineage", headers=setup.member.headers)
    assert tree.status_code == 200, tree.text
    return [e for e in tree.json()["edges"] if e["little_user_id"] == little_id]


@pytest.mark.asyncio
async def test_lineage_rejects_nonmembers_cross_chapter_and_inactive_targets(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Both endpoints of an edge must be active members of its chapter."""
    setup = await make_chapter_with("historian")
    other = await make_chapter_with("historian")
    outsider = await make_user("Lineage Outsider")
    inactive = await make_user("Inactive Lineage Member")
    await _join_as_member(client, setup, inactive)
    await _set_membership_status(inactive.id, setup.chapter_id, "inactive")

    nonmember_big = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": outsider.id, "little_user_id": setup.member.id},
        headers=setup.member.headers,
    )
    assert nonmember_big.status_code == 422
    assert nonmember_big.json()["detail"] == "lineage_target_not_in_chapter"

    cross_chapter_little = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": setup.member.id, "little_user_id": other.member.id},
        headers=setup.member.headers,
    )
    assert cross_chapter_little.status_code == 422
    assert cross_chapter_little.json()["detail"] == "lineage_target_not_in_chapter"

    inactive_little = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": setup.member.id, "little_user_id": inactive.id},
        headers=setup.member.headers,
    )
    assert inactive_little.status_code == 422
    assert inactive_little.json()["detail"] == "lineage_target_not_in_chapter"


@pytest.mark.asyncio
async def test_lineage_allows_ghost_targets_without_memberships(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Historical ghost users are the explicit exception to active membership."""
    setup = await make_chapter_with("historian")
    ghost_big = await make_user("Historical Big")
    ghost_little = await make_user("Historical Little")
    await _set_ghost(ghost_big.id)
    await _set_ghost(ghost_little.id)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": ghost_big.id, "little_user_id": ghost_little.id},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text


@pytest.mark.asyncio
async def test_lineage_replace_rejects_cross_chapter_big_and_keeps_old_edge(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Reassignment validates the new endpoint before replacing the old edge."""
    setup = await make_chapter_with("historian")
    other = await make_chapter_with("historian")
    old_big = await make_user("Old Big")
    little = await make_user("Lineage Little")
    await _join_as_member(client, setup, old_big)
    await _join_as_member(client, setup, little)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": old_big.id, "little_user_id": little.id},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text

    rejected = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={
            "big_user_id": other.member.id,
            "little_user_id": little.id,
            "replace_existing": True,
        },
        headers=setup.member.headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "lineage_target_not_in_chapter"
    survivors = await _edges_for(client, setup, little.id)
    assert len(survivors) == 1
    assert survivors[0]["id"] == created.json()["id"]
    assert survivors[0]["big_user_id"] == old_big.id


@pytest.mark.asyncio
async def test_replace_existing_reassigns_atomically_and_resets_confirmation(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """c79: fixing a wrong big is ONE call — and the little confirms the NEW big."""
    setup = await make_chapter_with("historian")
    wrong_big = await make_user("Wrong Big")
    right_big = await make_user("Right Big")
    little = await make_user("Little")
    for user in (wrong_big, right_big, little):
        await _join_as_member(client, setup, user)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": wrong_big.id, "little_user_id": little.id},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    # The little confirmed the WRONG pairing; the replace must not inherit this.
    confirmed = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges/{created.json()['id']}/confirm",
        headers=little.headers,
    )
    assert confirmed.status_code == 200

    replaced = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={
            "big_user_id": right_big.id,
            "little_user_id": little.id,
            "replace_existing": True,
        },
        headers=setup.member.headers,
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["big_user_id"] == right_big.id
    assert replaced.json()["confirmed_by_little"] is False

    edges = await _edges_for(client, setup, little.id)
    assert len(edges) == 1  # one big per little held through the replace
    assert edges[0]["big_user_id"] == right_big.id
    assert edges[0]["confirmed_by_little"] is False


@pytest.mark.asyncio
async def test_replace_that_would_cycle_leaves_old_edge_intact(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A rejected replace must not half-apply: the old pairing survives untouched."""
    setup = await make_chapter_with("historian")
    big = await make_user("Big")
    mid = await make_user("Mid")
    grandlittle = await make_user("Grandlittle")
    for user in (big, mid, grandlittle):
        await _join_as_member(client, setup, user)

    for big_id, little_id in ((big.id, mid.id), (mid.id, grandlittle.id)):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/lineage/edges",
            json={"big_user_id": big_id, "little_user_id": little_id},
            headers=setup.member.headers,
        )
        assert response.status_code == 201, response.text

    # Reassigning mid's big to mid's own little is a cycle even as a replace.
    cycle = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={
            "big_user_id": grandlittle.id,
            "little_user_id": mid.id,
            "replace_existing": True,
        },
        headers=setup.member.headers,
    )
    assert cycle.status_code == 422
    assert cycle.json()["detail"] == "lineage_cycle"

    edges = await _edges_for(client, setup, mid.id)
    assert len(edges) == 1
    assert edges[0]["big_user_id"] == big.id


@pytest.mark.asyncio
async def test_replace_within_family_chain_is_legal(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Moving a little one generation up their own chain must NOT read as a cycle —
    the cycle check has to run against the tree as it is AFTER the old edge is gone."""
    setup = await make_chapter_with("historian")
    grand = await make_user("Grand")
    parent = await make_user("Parent")
    little = await make_user("Little")
    for user in (grand, parent, little):
        await _join_as_member(client, setup, user)

    for big_id, little_id in ((grand.id, parent.id), (parent.id, little.id)):
        response = await client.post(
            f"/chapters/{setup.chapter_id}/lineage/edges",
            json={"big_user_id": big_id, "little_user_id": little_id},
            headers=setup.member.headers,
        )
        assert response.status_code == 201, response.text

    moved = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": grand.id, "little_user_id": little.id, "replace_existing": True},
        headers=setup.member.headers,
    )
    assert moved.status_code == 201, moved.text
    edges = await _edges_for(client, setup, little.id)
    assert len(edges) == 1
    assert edges[0]["big_user_id"] == grand.id


@pytest.mark.asyncio
async def test_delete_edge_unpairs_and_is_eboard_gated(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("historian")
    big = await make_user("Big")
    little = await make_user("Little")
    for user in (big, little):
        await _join_as_member(client, setup, user)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": big.id, "little_user_id": little.id},
        headers=setup.member.headers,
    )
    assert created.status_code == 201
    edge_id = created.json()["id"]

    # A plain member (the little themselves) cannot unpair.
    denied = await client.delete(
        f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}",
        headers=little.headers,
    )
    assert denied.status_code == 403

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}",
        headers=setup.member.headers,
    )
    assert deleted.status_code == 204
    assert await _edges_for(client, setup, little.id) == []

    # Gone means gone: a second delete is a 404, not a silent 204.
    again = await client.delete(
        f"/chapters/{setup.chapter_id}/lineage/edges/{edge_id}",
        headers=setup.member.headers,
    )
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_delete_edge_is_chapter_scoped(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """An e-board of chapter B reaching for chapter A's edge gets A's-shape 404."""
    owner = await make_chapter_with("historian")
    other = await make_chapter_with("historian")
    big = await make_user("Big")
    little = await make_user("Little")
    for user in (big, little):
        await _join_as_member(client, owner, user)

    created = await client.post(
        f"/chapters/{owner.chapter_id}/lineage/edges",
        json={"big_user_id": big.id, "little_user_id": little.id},
        headers=owner.member.headers,
    )
    assert created.status_code == 201
    edge_id = created.json()["id"]

    crossed = await client.delete(
        f"/chapters/{other.chapter_id}/lineage/edges/{edge_id}",
        headers=other.member.headers,
    )
    assert crossed.status_code == 404

    survivors = await _edges_for(client, owner, little.id)
    assert len(survivors) == 1
