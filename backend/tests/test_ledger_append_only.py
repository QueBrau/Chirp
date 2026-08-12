"""Ledger append-only (SPEC §8.2): no mutating routes, corrections, and the DB trigger."""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import MakeChapterWith

MUTATING_METHODS = {"PUT", "PATCH", "DELETE"}


def _iter_api_routes(routes: Iterable[Any]) -> Iterator[APIRoute]:
    """Yield every APIRoute, descending into lazily-included routers (FastAPI >= 0.141)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", route)
        nested = getattr(inner, "routes", None)
        if nested:
            yield from _iter_api_routes(nested)


def test_no_put_patch_delete_route_on_ledger_paths() -> None:
    """Introspect app.routes: ledger paths expose GET/POST only — no update/delete path exists."""
    from app.main import create_app

    app = create_app()
    ledger_routes = [
        route for route in _iter_api_routes(app.routes) if "ledger" in route.path
    ]
    assert ledger_routes, "expected ledger routes to be registered"
    for route in ledger_routes:
        exposed = (route.methods or set()) & MUTATING_METHODS
        assert not exposed, f"{route.path} exposes mutating methods {sorted(exposed)}"


async def test_post_ledger_entry_ok(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A treasurer can append a ledger entry via POST /chapters/{id}/ledger."""
    setup = await make_chapter_with("treasurer")

    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "expense",
            "amount_cents": -5000,
            "category": "social",
            "description": "Mixer decorations",
        },
        headers=setup.member.headers,
    )
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["chapter_id"] == setup.chapter_id
    assert entry["amount_cents"] == -5000
    assert entry["corrects_entry_id"] is None


async def test_correction_entry_references_original(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Corrections are new offsetting entries pointing at the original via corrects_entry_id."""
    setup = await make_chapter_with("treasurer")

    original = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={"entry_type": "expense", "amount_cents": -12000, "category": "formal"},
        headers=setup.member.headers,
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]

    correction = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": 12000,
            "description": "Reversing duplicate expense",
            "corrects_entry_id": original_id,
        },
        headers=setup.member.headers,
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["corrects_entry_id"] == original_id

    listing = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.member.headers
    )
    assert listing.status_code == 200
    assert {entry["id"] for entry in listing.json()} >= {original_id, correction.json()["id"]}


async def test_raw_sql_update_and_delete_raise_via_trigger(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The ledger_append_only trigger blocks UPDATE/DELETE even below the API layer."""
    from app.db import get_session_factory

    setup = await make_chapter_with("treasurer")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={"entry_type": "dues_payment", "amount_cents": 25000},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    entry_id = uuid.UUID(created.json()["id"])

    session_factory = get_session_factory()

    async with session_factory() as session:
        with pytest.raises(DBAPIError) as update_error:
            await session.execute(
                text("UPDATE ledger_entries SET amount_cents = 0 WHERE id = :id"),
                {"id": entry_id},
            )
        assert "append-only" in str(update_error.value)
        await session.rollback()

    async with session_factory() as session:
        with pytest.raises(DBAPIError) as delete_error:
            await session.execute(
                text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id}
            )
        assert "append-only" in str(delete_error.value)
        await session.rollback()

    # The row survived both attempts untouched.
    listing = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.member.headers
    )
    entries = {entry["id"]: entry for entry in listing.json()}
    assert entries[str(entry_id)]["amount_cents"] == 25000
