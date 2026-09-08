"""A rejected domain commit must never emit an accepted analytics transition."""
import json
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import get_session_factory


async def test_account_type_commit_failure_emits_no_change(client, make_user, caplog) -> None:
    user = await make_user("Analytics commit fixture", account_type="non_greek")
    async with get_session_factory()() as session:
        await session.execute(text("""
            CREATE FUNCTION c373_reject_account_change() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'c373 forced commit failure' USING ERRCODE='23514'; END $$
        """))
        await session.execute(text("""
            CREATE CONSTRAINT TRIGGER c373_account_change
            AFTER UPDATE ON users DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            WHEN (OLD.account_type IS DISTINCT FROM NEW.account_type)
            EXECUTE FUNCTION c373_reject_account_change()
        """))
        await session.commit()
    caplog.clear()
    try:
        with caplog.at_level(logging.INFO, logger="app.analytics"), pytest.raises(DBAPIError):
            await client.patch("/auth/me", json={"account_type": "alumni"}, headers=user.headers)
        changes = [r for r in caplog.records
                   if r.name == "app.analytics" and '"account_type_changed"' in r.getMessage()]
        assert changes == [], "a rejected transaction must not enter product analytics"
        me = await client.get("/auth/me", headers=user.headers)
        assert me.json()["user"]["account_type"] == "non_greek"
    finally:
        async with get_session_factory()() as session:
            await session.execute(text("DROP TRIGGER c373_account_change ON users"))
            await session.execute(text("DROP FUNCTION c373_reject_account_change()"))
            await session.commit()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.analytics"):
        response = await client.patch("/auth/me", json={"account_type": "alumni"}, headers=user.headers)
    assert response.status_code == 200
    changes = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.analytics"]
    assert [e["event"] for e in changes] == ["account_type_changed"]
