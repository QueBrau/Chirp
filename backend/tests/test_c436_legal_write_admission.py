"""Deletion holds fence assent writes while policy/privacy reads remain available."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from tests.test_c436_http_write_admission import _activate_hold, _set_admission_flag


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


async def _payload(client, *, minor=False):
    response = await client.get("/legal/policies")
    assert response.status_code == 200
    versions = {p["key"]: p["version"] for p in response.json()["policies"]}
    return {
        "terms_version": versions["terms"],
        "privacy_version": versions["privacy"],
        "age_declaration": 17 if minor else 18,
        "guardian_permission_confirmed": minor,
    }


async def _acceptance_count(user_id):
    async with get_session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(models.LegalAcceptance).where(
            models.LegalAcceptance.user_id == uuid.UUID(user_id)
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("minor", [False, True])
async def test_enforced_user_can_accept_before_product_access(client, make_user, monkeypatch, minor):
    _set_admission_flag(monkeypatch, True, legal_enforcement=True)
    user = await make_user("Assent user")
    before = await client.put("/alumni/profile", headers=user.headers, json={"company": "Before"})
    assert before.status_code == 428
    payload = await _payload(client, minor=minor)
    response = await client.post("/auth/legal-acceptance", headers=user.headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["required"] is False
    repeat = await client.post("/auth/legal-acceptance", headers=user.headers, json=payload)
    assert repeat.status_code == 200
    assert await _acceptance_count(user.id) == 2
    after = await client.put("/alumni/profile", headers=user.headers, json={"company": "After"})
    assert after.status_code == 200, after.text


@pytest.mark.asyncio
async def test_hold_rejects_assent_before_policy_sql_but_keeps_reads(client, make_user, monkeypatch):
    _set_admission_flag(monkeypatch, True, legal_enforcement=True)
    held = await make_user("Held assent")
    other = await make_user("Other assent")
    payload = await _payload(client)
    await _activate_hold(held.id)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await client.post("/auth/legal-acceptance", headers=held.headers, json=payload)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 423, response.text
    assert response.json() == {"detail": "account_deletion_in_progress"}
    assert not any("legal_policies" in sql or "legal_acceptances" in sql for sql in statements)
    assert await _acceptance_count(held.id) == 0
    assert (await client.get("/auth/legal-status", headers=held.headers)).status_code == 200
    assert (await client.get("/legal/policies")).status_code == 200
    assert (await client.get("/me/data-requests", headers=held.headers)).status_code == 200
    allowed = await client.post("/auth/legal-acceptance", headers=other.headers, json=payload)
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_legal_admission_preserves_suspension_version_and_guardian_rules(client, make_user, monkeypatch):
    _set_admission_flag(monkeypatch, True, legal_enforcement=True)
    user = await make_user("Assent validation")
    payload = await _payload(client)
    stale = await client.post("/auth/legal-acceptance", headers=user.headers, json={**payload, "terms_version": "old"})
    assert stale.status_code == 409
    minor = await client.post("/auth/legal-acceptance", headers=user.headers, json={**payload, "age_declaration": 17})
    assert minor.status_code == 422
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE users SET suspended_at = now() WHERE id = :id"), {"id": uuid.UUID(user.id)})
        await session.commit()
    suspended = await client.post("/auth/legal-acceptance", headers=user.headers, json=payload)
    assert suspended.status_code == 403
    assert suspended.json()["detail"] == "account_suspended"
    assert await _acceptance_count(user.id) == 0


@pytest.mark.asyncio
async def test_disabled_admission_does_not_query_hold_schema(client, make_user, monkeypatch):
    _set_admission_flag(monkeypatch, False, legal_enforcement=True)
    user = await make_user("Legacy assent")
    payload = await _payload(client)
    await _activate_hold(user.id)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await client.post("/auth/legal-acceptance", headers=user.headers, json=payload)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert not any("account_deletion_holds" in sql for sql in statements)


@pytest.mark.asyncio
async def test_hold_after_assent_commit_blocks_response_refresh(client, make_user, monkeypatch):
    _set_admission_flag(monkeypatch, True)
    user = await make_user("Postcommit assent")
    payload = await _payload(client)
    original_commit = AsyncSession.commit
    armed = True

    async def commit_then_hold(session):
        nonlocal armed
        await original_commit(session)
        if armed:
            armed = False
            await _activate_hold(user.id)

    monkeypatch.setattr(AsyncSession, "commit", commit_then_hold)
    response = await client.post("/auth/legal-acceptance", headers=user.headers, json=payload)
    assert response.status_code == 423, response.text
    # The first transaction succeeded; the new transaction must still recheck.
    assert await _acceptance_count(user.id) == 2
