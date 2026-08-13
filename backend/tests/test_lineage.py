"""Lineage: one-big-per-little, confirm flow, and tree payload shape."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


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
